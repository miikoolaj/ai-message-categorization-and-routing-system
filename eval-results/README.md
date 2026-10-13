# Raporty ewaluacji

Generowane przez `make eval` (skrypt `api/tests/eval/run_eval.py`, zbiór `api/tests/eval/dataset.yaml`).
Raporty Markdown są w repozytorium, surowe wyniki JSON są ignorowane przez git.

| Plik | Przebieg | Wynik |
|---|---|---|
| `qwen3_4b-instruct-2507-q4_K_M-20261013T105800.md` | Iteracja 0: prompt wyjściowy, zbiór strojeniowy (48) | 95,8% |
| `qwen3_4b-instruct-2507-q4_K_M-20261013T110644.md` | Iteracja 1: po dwóch ogólnych poprawkach, zbiór strojeniowy (48) | 100% |
| `qwen3_4b-instruct-2507-q4_K_M-20261013T110852.md` | Kontrola przeuczenia: zbiór `holdout` (10), uruchomiony raz | 100% |
