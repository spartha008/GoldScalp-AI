# Manual MT5 exports (raw data, local only)

These are verbatim copies of the XAUUSD bar exports made by hand from MetaTrader 5 (Vantage, *View → Symbols → Bars →
Export Bars*). The data is tab-separated, in MT5 server time, at bid prices. The originals are still on the Desktop.
`MANIFEST.json` lists, for every file:
- its SHA-256, row count and size;
- its range, taken from the MT5 file name;
- the research-store import that used it, matched by the SHA-256 the import recorded. All 13 files match.

| Folder | Used for | Research-store series |
|---|---|---|
| `2026-05_to_2026-10_phase5/` | Phase 5, 5.1 and 5.2: July–October 2026 (H1 from May, M1 for late September–October) | `csv:vantage-mt5-xauusd` |
| `2025-11_to_2026-05_phase5_3_validation/` | Phase 5.3 frozen validation: January–May 2026, plus warm-up | `csv:vantage-mt5-xauusd-2026-jan-may` |
| `2023-09_to_2025-10_phase6_sealed/` | The H6.4 sealed block: November 2023 – October 2025, plus warm-up. **Spent.** | `csv:vantage-mt5-xauusd-sealed-2023-2025` |

**Not in the public GitHub repository.** These are raw broker data (Vantage) and stay on this machine: the CSV files are
in `.gitignore`. Only this README and `MANIFEST.json` are published, so anyone can verify a copy by its hash.

Every period here has already been used in research, so none of it is fresh, independent validation data. Forward data
from 14 November 2026 onward is not in these files, and research code refuses to read it (FORWARD_DATA_GUARD_REPORT.md).
