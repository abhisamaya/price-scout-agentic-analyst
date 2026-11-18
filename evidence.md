# PriceScout — Project Evidence

**Project:** PriceScout: The AI-Powered Competitor Analyst
**Stack:** Anthropic Claude + Model Context Protocol (custom Firecrawl scraper, SQLite, filesystem)

## Architecture

- **`starter_server.py`** — custom FastMCP server exposing two tools:
  - `scrape_websites(websites, formats, api_key)` — scrapes each provider with Firecrawl, writes
    `scraped_content/{provider}_{format}.txt`, updates `scraped_metadata.json`, returns the list of
    providers that succeeded.
  - `extract_scraped_info(identifier)` — matches a provider name, URL or domain, loads the saved
    markdown/HTML into a `content` object, and returns formatted JSON (or a plain "no saved
    information" message).
- **`starter_client.py`** — Anthropic-powered orchestrator that discovers tools from every MCP
  server, runs the Claude tool-use loop with retries and a 60-second tool timeout, extracts
  structured pricing into SQLite, and prints stored plans.
- **`server_config.json`** — starts three MCP servers over stdio: the custom scraper (`uv`),
  SQLite (`uvx mcp-server-sqlite`), and filesystem (`npx`).

---

## Screenshot 1 — Multi-site scraping

**Query:**

```
scrape these sites: {'cloudrift': 'https://www.cloudrift.ai/inference', 'deepinfra': 'https://deepinfra.com/pricing', 'fireworks': 'https://fireworks.ai/pricing#serverless-pricing', 'groq': 'https://groq.com/pricing'}
```

**Result:** `✅ Successfully scraped 4 out of 4 websites: ['cloudrift', 'deepinfra', 'fireworks', 'groq']`

Each provider produced a `{provider}_markdown.txt` and `{provider}_html.txt` file, and all four
entries were written to `scraped_content/scraped_metadata.json` with provider name, URL, domain,
timestamp, formats, content file names, title, and description.

![Screenshot 1 — successful multi-site scrape](screenshot_1_scrape.png)

---

## Screenshot 2 — Comparison Q&A

**Query:** `Compare cloudrift ai and deepinfra's costs for deepseek v3`

**Answer summary:**

- **DeepInfra — DeepSeek-V3:** $0.32 input / $0.89 output per 1M tokens.
- **CloudRift AI:** its current inference page lists **DeepSeek-V4-Flash-0731** at
  $0.14 input / $0.28 output per 1M tokens. It does not list a DeepSeek-V3 SKU, so a direct
  like-for-like V3 comparison is not available from the scraped page.

The client delegated the request to the scraper server, used `extract_scraped_info` to read the
saved content, and answered from the real pricing tables.

![Screenshot 2 — comparison answer](screenshot_2_compare.png)

---

## Screenshot 3 — Data stored in SQLite

**Query:** `show data`

Pricing rows persisted by `DataExtractor.extract_and_store_data` (USD per 1M tokens):

| Company     | Plan                  | Input | Output |
|-------------|-----------------------|-------|--------|
| DeepInfra   | DeepSeek-V3           | 0.32  | 0.89   |
| DeepInfra   | DeepSeek-V3.1         | 0.25  | 0.95   |
| DeepInfra   | DeepSeek-V3.2         | 0.26  | 0.38   |
| CloudRift AI| DeepSeek-V4-Flash-0731| 0.14  | 0.28   |

![Screenshot 3 — show data](screenshot_3_show_data.png)

---

## How to run

```bash
cd Price_Scout_Agentic_Analyst
source .venv/bin/activate
python starter_client.py
```

Startup should report `Connected to 3 of 3 server(s)` and `Data extraction enabled`.

## Rubric coverage

1. **Custom MCP server**
   - `scrape_websites` creates one file per provider/format, maintains `scraped_metadata.json`,
     and returns a list whose length equals the number of successful scrapes.
   - `extract_scraped_info` looks up by provider name, URL, or domain and returns JSON with the
     loaded text for every saved format; unknown identifiers return plain text.
2. **AI-powered MCP client**
   - `Server.list_tools` validates the session and returns `name` / `description` / `input_schema`.
   - `Server.execute_tool` retries with backoff, logs each attempt, and calls
     `session.call_tool(..., read_timeout_seconds=timedelta(seconds=60))`.
   - `DataExtractor.extract_and_store_data` iterates `pricing_data["plans"]` and inserts into
     `pricing_plans` with the required column order and `json.dumps(features)`.
   - `ChatSession.process_query` uses `claude-sonnet-4-5-20250929`, accumulates `full_response`,
     appends assistant content, and runs the complete tool-use loop.
   - `ChatSession.show_stored_data` runs `read_query` and prints the formatted table above.
3. **Agentic workflow** — three MCP servers are connected; natural-language queries are delegated
   to scraping, extraction, and SQLite storage, and follow-ups answer from stored data.
