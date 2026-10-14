# Ewaluacja routingu - `qwen3.5:4b`

Data: 2026-10-14 00:38 UTC · przypadków: 52 · tryb agenta: `tool` · prompt: `16d66452` · polityka: `2026-10-13.3` · język tematu: `pl`

| Metryka | Wartość |
|---|---|
| Trafność (ścisła) | 98.1% (51/52) |
| Trafność (z alternatywami) | 98.1% (51/52) |
| Trafność bez przecieku z promptu | 98.1% (51/52), 95% CI Wilsona: 89.9-99.7% |
| Przypadki obecne w prompcie (`in-prompt`) | 0 |
| Tool-compliance (wysłał agent) | 100.0% (52/52) |
| Duplikaty wywołań narzędzia | 0.0% (0/52) |
| Decyzje poniżej progu pewności | 1.9% (1/52) |
| Średnio ponowień / zapytań do LLM | 0.00 / 2.00 |
| Czas p50 / p95 / max | 36.9 s / 40.1 s / 55.1 s |

## Kalibracja pewności

| Pewność modelu | Trafność |
|---|---|
| < 0.5 | 100.0% (1/1) |
| 0.5 - 0.8 | 100.0% (2/2) |
| >= 0.8 | 98.0% (48/49) |

## Trafność per dział (oczekiwany)

| Dział | Trafność |
|---|---|
| `help-desk@example.com` | 100.0% (11/11) |
| `human-resources@example.com` | 100.0% (13/13) |
| `it@example.com` | 88.9% (8/9) |
| `kadry@example.com` | 100.0% (10/10) |
| `other@example.com` | 100.0% (9/9) |

## Trafność per tag

| Tag | Trafność |
|---|---|
| clear | 100.0% (35/35) |
| contrast | 75.0% (3/4) |
| de | 100.0% (2/2) |
| en | 100.0% (5/5) |
| holdout | 98.1% (51/52) |
| injection | 100.0% (3/3) |
| long | 100.0% (1/1) |
| multi-intent | 100.0% (3/3) |
| no-diacritics | 100.0% (3/3) |
| nonsense | 100.0% (1/1) |
| pl | 97.6% (41/42) |
| security | 100.0% (3/3) |
| slang | 100.0% (3/3) |
| uk | 100.0% (2/2) |

## Jakość tekstów generowanych przez model

| Metryka | Wartość |
|---|---|
| Średnia długość tematu | 5.8 słów |
| Tematy w zakresie 2-12 słów | 100.0% (52/52) |
| Temat i uzasadnienie po polsku (heurystyka) | 100.0% (52/52) |
| Niepuste uzasadnienie | 100.0% (52/52) |

## Macierz pomyłek (wiersze: oczekiwany, kolumny: wybrany)

| oczekiwany \ wybrany | help-desk | human-resources | it | kadry | other | ERROR |
|---|---|---|---|---|---|---|
| **help-desk** | 11 | 0 | 0 | 0 | 0 | 0 |
| **human-resources** | 0 | 13 | 0 | 0 | 0 | 0 |
| **it** | 0 | 0 | 8 | 0 | 1 | 0 |
| **kadry** | 0 | 0 | 0 | 10 | 0 | 0 |
| **other** | 0 | 0 | 0 | 0 | 9 | 0 |

## Błędy (1)

- `it-14` (holdout - szczegóły ukryte, `--reveal-holdout` je pokaże)
