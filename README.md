# Investment dashboard

A phone-friendly Flask + SQLite dashboard for two independent portfolios. The administrator login can enter and edit both portfolios; the viewer login sees only its own portfolio. Records include holdings, cash accounts, interest, dividends, transactions, documents, allocation, historical performance and current account reconciliation.

**Deployment:** See [PORTAINER-SETUP.md](PORTAINER-SETUP.md) for the GitHub → Portainer → homelab walkthrough. The container serves HTTP on host port 3005 by default.

## Using the dashboard

Choose your portfolio from the selector at the top, then open **Manage**. Trades use a short form with type, date, account, holding, quantity, price, optional FX rate and brokerage. The cash activity tab covers deposits, withdrawals, dividends, interest, fees, transfers and currency exchanges; destination and received amount appear only when applicable. The account, holding, document and balance check lists have Edit and Delete controls. Prices and USD/AUD rates can also be corrected or deleted. Accounts with linked transactions, documents or balance checks cannot be deleted until those records are moved or removed, and a holding with linked trades or documents cannot be deleted. A holding shared by both portfolios is edited for both.

Start by selecting a portfolio in **Manage**. Add separate broker cash accounts for AUD and USD (for example Stake AUD and Stake USD), plus an investment bank account if needed. Add AU or US holdings, then enter the complete transaction history. Deposits and withdrawals are external contributions. Transfers between accounts do not count as returns. An FX transaction needs the source amount and the amount received in the destination currency. Buys and sells automatically affect the source account's cash. For a dividend reinvestment, record a gross dividend (with any withheld tax) and a matching buy. Sales exceeding the shares held in that broker account are rejected.

Upload PDFs, images and CSV files in **Manage** and attach each file to a holding, broker account and/or tax year. The Documents page filters them. A portfolio's viewer cannot access the other portfolio's documents by URL. The 20 MB file limit can be raised in `app.py` if needed.

## Market data and calculations

- `EODHD_API_KEY` enables US and Australian price history and delayed quotes, subject to your data plan. The separate worker fetches updates hourly by default. Without a key, enter prices manually in **Manage**. The worker fetches historical USD/AUD reference rates independently of the stock feed.
- Historical chart days with a missing holding price are left blank; no price is guessed. The dashboard displays the last price date and its source. The displayed 'change since yesterday' uses the previous calendar day and removes external deposits and withdrawals.
- Portfolio value includes holdings and cash converted to AUD. All-time profit is portfolio value less historical external contributions, including recorded dividends, interest, trading costs and fees. Unrealised holding gain is based on a pooled average cost **within each broker account**. These calculations are for monitoring, not tax reporting.
- The Reconcile page compares today's broker-reported account value with the current estimated cash plus holdings in that same account. Historical entries are stored as records but are not automatically checked against a dated account snapshot.

Back up the entire data directory, including the SQLite database and the `documents` folder. The included backup service makes a daily online SQLite copy and copies documents to the separately mounted backup directory, retaining 30 days by default. A document uploaded during the copy may only appear in the next backup; keep another copy of irreplaceable statements.

Transactions can be exported as CSV by the administrator. Edits and deletions are recorded in `transaction_audit` in the database. Each user can change their password in **Cash & accounts**.

## Updating an existing Portainer installation

Replace the repository files and redeploy the stack from its GitHub source. Keep the same Portainer environment variables and mounted data and backup directories. The application adds its new transaction FX column automatically on startup; do not delete the SQLite database or the documents directory. If Portainer offers **Re-pull image**, leave it off: this stack builds the local `investment-dashboard:local` image from the repository rather than pulling it from a registry.
