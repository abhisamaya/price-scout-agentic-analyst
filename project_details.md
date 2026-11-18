Project Introduction
PriceScout: The AI-Powered Competitor Analyst
For this project, you're a Senior Strategy Analyst at a new, fast-growing AI company. Your company builds infrastructure for developers to run large language models, and you're in a market with tons of competition. Everyone is changing their prices, adding new models, and updating features every week.

The Problem You Face
Right now, you spend more than half your week manually checking competitor websites. You have a spreadsheet open, and you're bouncing between tabs, copying and pasting pricing for models like 'Llama 3' or 'Deepseek v3' from different sites. It's slow, it's tedious, and by the time you finish your report, the data is already old. Your team can't make fast, smart decisions about your own pricing and product strategy because you're always playing catch-up.

Your Mission: Build an Automated Intelligence Bot
Your goal is to build a tool that does this work for you. We're going to create a chatbot that you can simply ask questions to, like:

"How much does DeepInfra charge for Deepseek v3?"
"Go find the pricing for Groq."
"Compare the prices for CloudRift AI and DeepInfra."


Behind the scenes, this bot will use AI to understand your question, use tools to browse the web, scrape the relevant data, and store it in a database for future use.

How You'll Build It with MCP :
This is where the Model Context Protocol (MCP) becomes so useful. Instead of building one giant, complicated application, you're going to use MCP to connect several small, specialized "agents" or servers.

The LLM Client: This is our "manager" agent. We'll use the Anthropic Claude model. Its job is to understand our natural language questions and decide which tools to use.

The Scraper Server: This is our "web researcher." We will build this custom MCP server ourselves using Python. Its job is to take a URL, use the Firecrawl service to scrape its content, and save that data.

The Database Server: This is our "memory." We'll use a pre-built MCP server for SQLite. Its job is to store the data our scraper finds so we can ask questions about it later without having to scrape the same site again.

You're not just building a chatbot; you're building an entire system of agents that work together.

Assessment
Make sure to check the rubric as you complete this project.


Assesment ruberics :

1. Custom MCP Server
Criteria 1 - Implement scrape_websites tool to scrape, persist, and track metadata
Submission Requirements - Running the tool creates scraped_content/ with one file per {provider}_{format}.txt for each successful provider and creates/updates scraped_metadata.json containing provider name, url, domain, scraped_at, content_files, title, description. Tool returns a list whose length equals number of successful scrapes.

Criteria 2 - Implement extract_scraped_info tool to load metadata and return file contents
Submission Requirements - Calling the tool with an identifier that matches provider name, URL, or domain returns a formatted JSON string whose content field includes loaded text for each available format; non-matching identifier returns a plain-text message indicating no saved information.

2. AI-Powered MCP Client
Criteria 1 : List available MCP tools
Submission Requirements - Execute tools with retries : Server.list_tools checks session existence, calls list_tools(), and returns a list of dicts containing name, description, input_schema

Criteria 2 : Execute tools with retries
Submission Requirements - Server.execute_tool contains a retry loop and within each attempt logs execution, calls session.call_tool(...) with a 60-second read timeout, and returns the result on success.

Criteria 3 : Persist structured pricing to SQLite via MCP
Submission Requirements - DataExtractor.extract_and_store_data iterates pricing_data["plans"] and executes the provided write_query insertion exactly as specified (including column order and json.dumps for features).

Criteria 4 : Drive tool use via LLM and complete tool-use loop
Submission Requirements - ChatSession.process_query sets a real model name (e.g., claude-3-5-sonnet-20240620), appends text chunks to full_response, appends assistant content, handles single-text completion exit, and for tool_use performs the full 1–7 loop (append tool request, locate server, execute tool, append tool result, call model again, and exit if next response is text only).

Criteria 5 : Display recently stored data from DB
Submission Requirements - | data from DB |
| ChatSession.show_stored_data executes the provided read_query, prints the header lines, iterates result rows, and prints the formatted bullet lines including company, plan, and token pricing, then a closing separator. |



3. Orchestrate an Agentic Workflow
Criteria 1 : Orchestrate a functional multi-agent MCP workflow
Submission Requirements - The running client connects to three MCP servers (custom scraper, SQLite, filesystem where applicable) and, from natural-language queries, delegates to the correct tools to scrape, analyze, and store competitor pricing, then answer follow-ups using stored data.

Criteria 2 : Demonstrate comparison Q&A
Submission Requirements - Screenshot showing the comparison query Compare cloudrift ai and deepinfra's costs for deepseek v3 and a full natural-language answer.

Criteria 3 : Demonstrate scraping success
Submission Requirements - **Screenshot **showing the scrape these sites: {...} query and output confirming success count (e.g., “Successfully scraped 4 out of 4 websites” or equivalent success log).


4. Other requirements (Optional):
* Add a retry/backoff and robots-aware crawl policy with per-provider rate limits and deduplication.
* Implement a caching layer: check scraped_metadata.json timestamps and skip re-scraping unless stale, with a --force option.
* Add a “pricing change alert” tool that diffs current vs. prior scrapes and summarizes changes in the response.
* Add unit tests for the two tools and a smoke test that validates DB schema and a full scrape-to-answer flow.
* Build a small CLI/terminal UI that streams tool events (scrape → parse → DB write → answer) for transparency.

