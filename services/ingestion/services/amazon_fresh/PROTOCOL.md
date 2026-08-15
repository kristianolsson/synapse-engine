# Groceries Protocol

## Tool

To interact with groceries, like grocery item searches and cart operations, you MUST run the `amazon-fresh` tool, which is globally available in your environment.
Run `amazon-fresh --help` and `amazon-fresh <command> --help` to discover all available commands and arguments. Never invoke the tool without running `--help` first, even if you believe you know the subcommand or flag names.

## Item Resolution

For each requested item, follow this exact sequence:

### 1. History Lookup

Load `groceries/grocery_history.json` and find the entry whose `generic_name` best matches the user's request (fuzzy match — e.g., "milk" → `milk`, "bananas" → `bananas`).

- Match found with a non-null `asin` → run `amazon-fresh add <asin>`. Add silently, no confirmation needed. If add returns that the item is out of stock or wasn't added, proceed to **Past Purchases**.
- Match found but `asin` is null, or no match found → proceed to **Past Purchases**.

### 2. Past Purchases

Run `amazon-fresh past-purchases` **once per session** — lazily, only if at least one item requires it. Fuzzy-match the item against results, prioritize most recent purchases. If an item is out of stock, see if there are similar items that was previously purchased, otherwise proceed to **Search**.

- Match found → present to user for confirmation (name and price). If there primary option was out of stock, mention that in the confirmation message, and suggest the similar item that was previously purchased instead. On user confirm: add to cart. 
- No match → proceed to **Search**.

### 3. Search

Run `amazon-fresh search <query>` using the item's `generic_name` (or `options[0]` name if present, otherwise the user's original phrasing). Present the top 2–3 results with name and price and ask the user to confirm. Do NOT add to cart until confirmed. Prioritize items that have been purchased before (most recent first).

On confirm: add to cart.

## Out of Stock

If `amazon-fresh add` returns an out-of-stock or not-found error at any step, fall through to **Search** and present live in-stock options for confirmation, prioritize items that were previously purchased (most recent first).

## Quantity

Default quantity is **1** unless the user specifies otherwise (e.g., "add 2 milks" → qty 2).

## Bulk List Processing

When the user provides multiple items in one message, process all items. Collect everything requiring confirmation (past purchases matches, search results, out-of-stock fallbacks) and present in a **single message** — do not ask per-item. Add confirmed items after the user responds.

## history.json Schema

```json
{
  "generic_name": "bananas",
  "asin": "B07ZLF9G83",
  "options": [
    { "name": "Organic Banana Bunch (4-5 Count)" },
    { "name": "Banana Bunch (4-5 Count)" }
  ],
  "note": null
}
```

- `asin`: id to use for product, fallback to search on name if null. 
- `options`: ordered name hints used as search queries when `asin` is missing or out of stock.

## Cart

The user manages checkout themselves. After adding items, do not prompt for cart review or checkout confirmation — just report what was done.

## Output Format

Follow the standard Synapse channel output rules (HTML for Telegram/email). For grocery results:

- **Silently added items**: confirm name and price
- **Pending confirmations**: present options with name and price, ask user to pick
- **Out-of-stock fallbacks**: note the original item, present search results
