import asyncio
import json
import logging
import os
import shutil
from contextlib import AsyncExitStack
from typing import Any, List, Dict, TypedDict, Optional
from datetime import datetime, timedelta
from pathlib import Path
import re
import ast

from dotenv import load_dotenv
from anthropic import Anthropic
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

class ToolDefinition(TypedDict):
    name: str
    description: str
    input_schema: dict


def tool_result_text(result: Any) -> str:
    """Flatten an MCP CallToolResult into a plain string."""
    if result is None:
        return ""
    if isinstance(result, str):
        return result
    content = getattr(result, "content", None)
    if content is None:
        return str(result)
    parts: list[str] = []
    for item in content:
        text = getattr(item, "text", None)
        if text is None and isinstance(item, dict):
            text = item.get("text")
        if text:
            parts.append(str(text))
    return "\n".join(parts) if parts else str(result)


class Configuration:
    """Manages configuration and environment variables for the MCP client."""

    def __init__(self) -> None:
        """Initialize configuration with environment variables."""
        self.load_env()
        self.api_key = os.getenv("ANTHROPIC_API_KEY")

    @staticmethod
    def load_env() -> None:
        """Load environment variables from .env file."""
        load_dotenv()

    @staticmethod
    def load_config(file_path: str | Path) -> dict[str, Any]:
        """Load server configuration from JSON file.

        Args:
            file_path: Path to the JSON configuration file.

        Returns:
            Dict containing server configuration.

        Raises:
            FileNotFoundError: If configuration file doesn't exist.
            JSONDecodeError: If configuration file is invalid JSON.
            ValueError: If configuration file is missing required fields.
        """
        
        path = Path(file_path)
        if not path.is_file():
            raise FileNotFoundError(f"Configuration file not found: {path}")

        try:
            with open(path, "r", encoding="utf-8") as handle:
                config = json.load(handle)
        except json.JSONDecodeError as exc:
            raise json.JSONDecodeError(
                f"Invalid JSON in {path}: {exc.msg}", exc.doc, exc.pos
            ) from exc

        if not isinstance(config, dict) or "mcpServers" not in config:
            raise ValueError(f"Configuration file {path} must contain a 'mcpServers' object")
        if not isinstance(config["mcpServers"], dict) or not config["mcpServers"]:
            raise ValueError("'mcpServers' must be a non-empty object")

        for name, server_config in config["mcpServers"].items():
            if not isinstance(server_config, dict) or "command" not in server_config:
                raise ValueError(f"Server '{name}' is missing a 'command' field")

        logging.info(f"✓ Loaded {len(config['mcpServers'])} server(s) from {path.name}")
        return config

    @property
    def anthropic_api_key(self) -> str:
        """Get the Anthropic API key.

        Returns:
            The API key as a string.

        Raises:
            ValueError: If the API key is not found in environment variables.
        """
        if not self.api_key:
            raise ValueError("ANTHROPIC_API_KEY not found in environment variables")
        return self.api_key


class Server:
    """Manages MCP server connections and tool execution."""

    def __init__(self, name: str, config: dict[str, Any]) -> None:
        self.name: str = name
        self.config: dict[str, Any] = config
        self.stdio_context: Any | None = None
        self.session: ClientSession | None = None
        self._cleanup_lock: asyncio.Lock = asyncio.Lock()
        self.exit_stack: AsyncExitStack = AsyncExitStack()

    async def initialize(self) -> None:
        """Initialize the server connection."""
        command = shutil.which(self.config["command"])
        if command is None:
            raise ValueError(f"Command not found on PATH: {self.config['command']} (is Node.js / uv installed?)")

        # complete params
        env = dict(os.environ)
        if self.config.get("env"):
            env.update({str(k): str(v) for k, v in self.config["env"].items()})

        server_params = StdioServerParameters(
            command=command,
            args=list(self.config.get("args", [])),
            env=env,
            cwd=self.config.get("cwd", str(Path(__file__).parent)),
        )

        try:
            stdio_transport = await self.exit_stack.enter_async_context(stdio_client(server_params))
            read, write = stdio_transport
            session = await self.exit_stack.enter_async_context(ClientSession(read, write))
            await session.initialize()
            self.session = session
            logging.info(f"✓ Server '{self.name}' initialized")
        except Exception as e:
            logging.error(f"Error initializing server {self.name}: {e}")
            await self.cleanup()
            raise

    async def list_tools(self) -> List[ToolDefinition]:
        """List available tools from the server.

        Returns:
            A list of available tool definitions.

        Raises:
            RuntimeError: If the server is not initialized.
        """
        # complete
        if not self.session:
            raise RuntimeError(f"Server '{self.name}' is not initialized")

        response = await self.session.list_tools()
        tools: List[ToolDefinition] = []
        for tool in response.tools:
            tools.append({
                "name": tool.name,
                "description": tool.description or "",
                "input_schema": tool.inputSchema or {"type": "object", "properties": {}},
            })
        logging.info(f"✓ {len(tools)} tool(s) loaded from '{self.name}'")
        return tools

    async def execute_tool(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        retries: int = 2,
        delay: float = 1.0,
    ) -> Any:
        """Execute a tool with retry mechanism.

        Args:
            tool_name: Name of the tool to execute.
            arguments: Tool arguments.
            retries: Number of retry attempts.
            delay: Delay between retries in seconds.

        Returns:
            Tool execution result.

        Raises:
            RuntimeError: If server is not initialized.
            Exception: If tool execution fails after all retries.
        """
        if not self.session:
            raise RuntimeError(f"Server '{self.name}' is not initialized")

        for attempt in range(1, retries + 1):
            try:
                logging.info(
                    f"Executing tool '{tool_name}' on '{self.name}' "
                    f"(attempt {attempt}/{retries})"
                )
                result = await self.session.call_tool(
                    tool_name,
                    arguments=arguments,
                    read_timeout_seconds=timedelta(seconds=60),
                )
                logging.info(f"✓ Tool '{tool_name}' completed")
                return result
            except Exception as e:
                logging.warning(
                    f"Tool '{tool_name}' failed on attempt {attempt}/{retries}: {e}"
                )
                if attempt >= retries:
                    raise
                await asyncio.sleep(delay * attempt)

    async def cleanup(self) -> None:
        """Clean up server resources."""
        async with self._cleanup_lock:
            try:
                await self.exit_stack.aclose()
                self.session = None
                self.stdio_context = None
            except Exception as e:
                logging.error(f"Error during cleanup of server {self.name}: {e}")


class DataExtractor:
    """Handles extraction and storage of structured data from LLM responses."""

    def __init__(self, sqlite_server: Server, anthropic_client: Anthropic):
        self.sqlite_server = sqlite_server
        self.anthropic = anthropic_client

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _sql_literal(value: Any) -> str:
        """Render a Python value as a safely escaped SQLite literal.

        mcp-server-sqlite exposes only write_query(query: str), so ? placeholders
        cannot be used; values have to be inlined and escaped here.
        """
        if value is None:
            return "NULL"
        if isinstance(value, bool):
            return "1" if value else "0"
        if isinstance(value, (int, float)):
            return repr(value) if isinstance(value, float) else str(value)
        return "'" + str(value).replace("'", "''") + "'"

    @staticmethod
    def _to_number(value: Any) -> Optional[float]:
        """Coerce values like '$0.27 per 1M' into 0.27; return None if unknown."""
        if value is None or isinstance(value, bool):
            return None
        if isinstance(value, (int, float)):
            return float(value)
        if isinstance(value, str):
            cleaned = value.replace(",", "").strip()
            exact = re.fullmatch(r"-?\d+(?:\.\d+)?", cleaned)
            if exact:
                return float(cleaned)
            match = re.search(r"-?\d+(?:\.\d+)?", cleaned)
            if match:
                return float(match.group(0))
        return None

    @staticmethod
    def _parse_json_object(raw: str) -> Dict[str, Any]:
        """Parse a JSON object even if the model wrapped it in fences or prose."""
        text = (raw or "").strip()
        if not text:
            raise ValueError("Empty extraction response")

        if text.startswith("```"):
            text = text.strip("`").strip()
            if text[:4].lower() == "json":
                text = text[4:].strip()

        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end <= start:
            raise ValueError(f"No JSON object found in extraction response: {text[:200]!r}")

        data = json.loads(text[start:end + 1])
        if not isinstance(data, dict):
            raise ValueError(f"Expected a JSON object, got {type(data).__name__}")
        return data

    # ------------------------------------------------------------------
    # Table setup
    # ------------------------------------------------------------------

    async def setup_data_tables(self) -> None:
        """Setup tables for storing extracted data."""
        try:
            await self.sqlite_server.execute_tool("write_query", {
                "query": """
                CREATE TABLE IF NOT EXISTS pricing_plans (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    company_name TEXT NOT NULL,
                    plan_name TEXT NOT NULL,
                    input_tokens REAL,
                    output_tokens REAL,
                    currency TEXT DEFAULT 'USD',
                    billing_period TEXT,  -- 'monthly', 'yearly', 'one-time'
                    features TEXT,  -- JSON array
                    limitations TEXT,
                    source_query TEXT,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
                )
                """
            })

            logger.info("✓ Data extraction tables initialized")

        except Exception as e:
            logger.error(f"Failed to setup data tables: {e}")

    # ------------------------------------------------------------------
    # LLM extraction
    # ------------------------------------------------------------------

    async def _get_structured_extraction(self, prompt: str) -> str:
        """Use Claude to extract structured data."""
        try:
            response = self.anthropic.messages.create(
                max_tokens=2048,
                model='claude-sonnet-4-5-20250929',
                messages=[{'role': 'user', 'content': prompt}]
            )

            text_content = ""
            for content in response.content:
                if content.type == 'text':
                    text_content += content.text

            return text_content.strip()

        except Exception as e:
            logger.error(f"Error in structured extraction: {e}")
            return '{"error": "extraction failed"}'

    async def extract_and_store_data(self, user_query: str, llm_response: str,
                                     source_url: str = None) -> None:
        """Extract structured data from LLM response and store it."""
        if not llm_response.strip():
            logger.info("Nothing to extract: empty LLM response")
            return

        try:
            extraction_prompt = f"""
            Analyze this text and extract pricing information in JSON format:

            Text: {llm_response}

            Extract pricing plans with this structure:
            {{
                "company_name": "company name (used as the default for plans)",
                "plans": [
                    {{
                        "company_name": "company name for this plan",
                        "plan_name": "plan or model name",
                        "input_tokens": number or null,
                        "output_tokens": number or null,
                        "currency": "USD",
                        "billing_period": "per 1M tokens / monthly / yearly / one-time",
                        "features": ["feature1", "feature2"],
                        "limitations": "any limitations mentioned",
                        "query": "the user's query"
                    }}
                ]
            }}

            Rules:
            - input_tokens and output_tokens must be USD per 1 million tokens.
              If the text gives a per-token price, multiply it by 1,000,000.
            - Emit one plan object per company/model combination. Always set
              company_name on the plan when the text compares several companies.
            - Return only valid JSON, no other text. Do not return your response
              enclosed in ```json```
            """

            extraction_response = await self._get_structured_extraction(extraction_prompt)
            pricing_data = self._parse_json_object(extraction_response)

            if "error" in pricing_data and "plans" not in pricing_data:
                logger.warning(f"Structured extraction failed: {pricing_data.get('error')}")
                return

            plans = pricing_data.get("plans") or []
            if not isinstance(plans, list):
                logger.warning("Extraction returned a non-list 'plans' field; skipping")
                return

            default_company = pricing_data.get("company_name") or "Unknown"
            stored = 0

            for plan in plans:
                if not isinstance(plan, dict):
                    continue
                try:
                    company_name = plan.get("company_name") or default_company
                    plan_name = plan.get("plan_name") or "Unknown"

                    input_tokens = self._to_number(plan.get("input_tokens"))
                    output_tokens = self._to_number(plan.get("output_tokens"))
                    if input_tokens is None and output_tokens is None:
                        logger.info(f"Skipping {company_name!r} / {plan_name!r}: no numeric pricing")
                        continue

                    features = plan.get("features") or []
                    if isinstance(features, str):
                        try:
                            features = json.loads(features)
                        except json.JSONDecodeError:
                            features = [features]
                    if not isinstance(features, list):
                        features = [str(features)]
                    features_json = json.dumps(features)

                    insert_query = f"""
                    INSERT INTO pricing_plans (
                        company_name, plan_name, input_tokens, output_tokens,
                        currency, billing_period, features, limitations, source_query
                    ) VALUES (
                        {self._sql_literal(company_name)},
                        {self._sql_literal(plan_name)},
                        {self._sql_literal(input_tokens)},
                        {self._sql_literal(output_tokens)},
                        {self._sql_literal(plan.get("currency") or "USD")},
                        {self._sql_literal(plan.get("billing_period"))},
                        {self._sql_literal(features_json)},
                        {self._sql_literal(plan.get("limitations"))},
                        {self._sql_literal(user_query)}
                    )
                    """

                    await self.sqlite_server.execute_tool("write_query", {"query": insert_query})
                    stored += 1

                except Exception as plan_error:
                    logger.error(f"Failed to store plan {plan.get('plan_name')!r}: {plan_error}")

            logger.info(
                f"Stored {stored} of {len(plans)} pricing plans"
                + (f" (source: {source_url})" if source_url else "")
            )

        except Exception as e:
            logger.error(f"Error extracting pricing data: {e}")


class ChatSession:
    """Orchestrates the interaction between user, LLM, and tools."""

    def __init__(self, servers: list[Server], api_key: str) -> None:
        self.servers: list[Server] = servers
        self.anthropic = Anthropic(api_key=api_key)
        self.available_tools: List[ToolDefinition] = []
        self.tool_to_server: Dict[str, str] = {}
        self.sqlite_server: Server | None = None
        self.data_extractor: DataExtractor | None = None

    async def cleanup_servers(self) -> None:
        """Clean up all servers properly."""
        for server in reversed(self.servers):
            try:
                await server.cleanup()
            except Exception as e:
                logging.warning(f"Warning during final cleanup: {e}")

    @staticmethod
    def _parse_provider_list(text: str) -> List[str] | None:
        """Parse the scraper's list of provider names from a tool result."""
        for parser in (json.loads, ast.literal_eval):
            try:
                data = parser(text)
            except (ValueError, SyntaxError, TypeError):
                continue
            if isinstance(data, list):
                return [str(item) for item in data]
        return None

    async def process_query(self, query: str) -> None:
        """Process a user query and extract/store relevant data."""
        messages = [{'role': 'user', 'content': query}]
        response = self.anthropic.messages.create(
            max_tokens=2024,
            model='claude-sonnet-4-5-20250929',
            tools=self.available_tools,
            messages=messages
        )

        full_response = ""
        source_url = None
        used_web_search = False

        process_query = True
        while process_query:
            assistant_content = []
            tool_requests = []

            for content in response.content:
                if content.type == 'text':
                    print(content.text)
                    full_response += content.text
                    assistant_content.append(content)
                elif content.type == 'tool_use':
                    assistant_content.append(content)
                    tool_requests.append(content)

            if assistant_content:
                messages.append({'role': 'assistant', 'content': assistant_content})

            # No tool calls: this was the final natural-language answer.
            if not tool_requests:
                process_query = False
                break

            tool_results = []
            for tool_request in tool_requests:
                tool_name = tool_request.name
                tool_args = dict(tool_request.input or {})
                server_name = self.tool_to_server.get(tool_name)

                print(f"\n🔧 Calling {tool_name}({json.dumps(tool_args)[:250]})")

                if server_name is None:
                    tool_output = f"Error: unknown tool '{tool_name}'"
                else:
                    server = next((s for s in self.servers if s.name == server_name), None)
                    try:
                        if server is None:
                            raise RuntimeError(f"Server '{server_name}' is not available")
                        result = await server.execute_tool(tool_name, tool_args)
                        tool_output = tool_result_text(result)
                    except Exception as e:
                        tool_output = f"Error executing {tool_name}: {e}"

                if tool_name == "scrape_websites":
                    used_web_search = True
                    websites = tool_args.get("websites") or {}
                    if isinstance(websites, dict) and websites:
                        source_url = list(websites.values())[0]
                    requested = len(websites) if isinstance(websites, dict) else 0
                    scraped = self._parse_provider_list(tool_output)

                    if scraped:
                        print(
                            f"✅ Successfully scraped {len(scraped)} out of {requested} "
                            f"websites: {scraped}"
                        )
                        tool_output = (
                            f"Successfully scraped {len(scraped)} out of {requested} "
                            f"websites: {scraped}\n"
                            "Use extract_scraped_info with each provider name to read "
                            "the scraped pricing text."
                        )
                    elif scraped is not None:
                        print(f"⚠️ Scraped 0 out of {requested} websites")
                        tool_output = (
                            f"Scraping failed for all {requested} websites. "
                            "Check the server logs for the Firecrawl error."
                        )
                else:
                    preview = tool_output
                    if len(preview) > 800:
                        preview = preview[:800] + "\n...[truncated for display]"
                    print(preview)

                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": tool_request.id,
                    "content": tool_output,
                })

            messages.append({'role': 'user', 'content': tool_results})

            response = self.anthropic.messages.create(
                max_tokens=2024,
                model='claude-sonnet-4-5-20250929',
                tools=self.available_tools,
                messages=messages
            )

        if self.data_extractor and full_response.strip():
            await self.data_extractor.extract_and_store_data(query, full_response.strip(), source_url)

    def _extract_url_from_result(self, result_text: str) -> str | None:
        """Extract URL from tool result."""
        url_pattern = r'https?://[^\s<>"{}|\\^`\[\]]+'
        urls = re.findall(url_pattern, result_text)
        return urls[0] if urls else None

    async def chat_loop(self) -> None:
        """Run an interactive chat loop."""
        print("\nMCP Chatbot with Data Extraction Started!")
        print("Type your queries, 'show data' to view stored data, or 'quit' to exit.")
        
        while True:
            try:
                query = input("\nQuery: ").strip()
        
                if query.lower() == 'quit':
                    break
                elif query.lower() == 'show data':
                    await self.show_stored_data()
                    continue
                    
                await self.process_query(query)
                print("\n")
                    
            except KeyboardInterrupt:
                print("\nExiting...")
                break
            except Exception as e:
                print(f"\nError: {str(e)}")

    async def show_stored_data(self) -> None:
        """Show recently stored data."""
        if not self.sqlite_server:
            logger.info("No database available")
            return

        def format_price(value: Any) -> str:
            try:
                return f"{float(value):g}"
            except (TypeError, ValueError):
                return "n/a"

        try:
            read_query = """
            SELECT company_name, plan_name, input_tokens, output_tokens,
                   currency, billing_period, features, created_at
            FROM pricing_plans
            ORDER BY created_at DESC, id DESC
            LIMIT 20
            """
            result = await self.sqlite_server.execute_tool("read_query", {"query": read_query})
            raw = tool_result_text(result)

            # mcp-server-sqlite returns a Python-literal list of dicts, not JSON,
            # so accept both representations.
            rows = None
            for parser in (json.loads, ast.literal_eval):
                try:
                    candidate = parser(raw)
                except (ValueError, SyntaxError, TypeError):
                    continue
                if isinstance(candidate, list):
                    rows = candidate
                    break

            if rows is None:
                print(raw)
                return

            print("=" * 78)
            print("RECENTLY STORED PRICING DATA")
            print("=" * 78)

            if not rows:
                print("No pricing data stored yet.")
                print("=" * 78)
                return

            for row in rows:
                company = row.get("company_name") or "Unknown"
                plan = row.get("plan_name") or "Unknown"
                currency = row.get("currency") or "USD"
                period = row.get("billing_period") or "per 1M tokens"

                print(f"• {company} | {plan}")
                print(
                    f"  Input: {format_price(row.get('input_tokens'))} {currency} ({period})"
                    f" | Output: {format_price(row.get('output_tokens'))} {currency} ({period})"
                )

                features = row.get("features")
                if features:
                    try:
                        feature_list = json.loads(features) if isinstance(features, str) else features
                    except (json.JSONDecodeError, TypeError):
                        feature_list = None
                    if feature_list:
                        print(f"  Features: {', '.join(map(str, feature_list))[:120]}")

                print("-" * 78)

            print(f"Showing {len(rows)} most recent plan(s)")
            print("=" * 78)

        except Exception as e:
            print(f"Error showing data: {e}")

    async def start(self) -> None:
        """Main chat session handler."""
        active_servers: list[Server] = []
        try:
            for server in self.servers:
                try:
                    await server.initialize()
                    active_servers.append(server)
                    if "sqlite" in server.name.lower():
                        self.sqlite_server = server
                except Exception as e:
                    logging.error(f"Failed to initialize server '{server.name}': {e}")

            if not active_servers:
                print("No MCP servers could be started - check server_config.json and your PATH.")
                return

            for server in active_servers:
                tools = await server.list_tools()
                self.available_tools.extend(tools)
                for tool in tools:
                    self.tool_to_server[tool["name"]] = server.name

            print(f"\nConnected to {len(active_servers)} of {len(self.servers)} server(s)")
            print(f"Available tools: {[tool['name'] for tool in self.available_tools]}")

            if not self.sqlite_server:
                print("WARNING: SQLite server unavailable - 'show data' and storage are disabled.")
            else:
                self.data_extractor = DataExtractor(self.sqlite_server, self.anthropic)
                await self.data_extractor.setup_data_tables()
                print("Data extraction enabled")

            await self.chat_loop()

        finally:
            await self.cleanup_servers()


async def main() -> None:
    """Initialize and run the chat session."""
    config = Configuration()
    
    script_dir = Path(__file__).parent
    config_file = script_dir / "server_config.json"
    
    server_config = config.load_config(config_file)
    
    servers = [Server(name, srv_config) for name, srv_config in server_config["mcpServers"].items()]
    chat_session = ChatSession(servers, config.anthropic_api_key)
    await chat_session.start()


if __name__ == "__main__":
    asyncio.run(main())