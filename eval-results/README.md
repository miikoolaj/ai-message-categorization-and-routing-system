# Raporty ewaluacji

Generowane przez `make eval` (skrypt `api/tests/eval/run_eval.py`, zbiór `api/tests/eval/dataset.yaml`).
Raporty Markdown są w repozytorium, surowe wyniki JSON są ignorowane przez git. Nagłówek każdego
raportu podaje model, tryb agenta, wersję promptu (`prompt_version`) i wersję polityki działów.

## Zbiór 158 przypadków (52 holdout)

| Plik | Przebieg | Trafność (95% CI) | Holdout |
|---|---|---|---|
| `qwen3_4b-instruct-2507-q4_K_M-tool-20261013T174841.md` | Tryb `tool`, polityka `.2` (pierwszy przebieg na nowym zbiorze) | 94,9% (90,3–97,4%) | 49/52 |
| `qwen3_4b-instruct-2507-q4_K_M-output_function-20261013T231743.md` | Tryb `output_function`, polityka `.2` (jedna runda LLM, mediana 9,1 s zamiast 12,3 s) | 94,9% (90,3–97,4%) | 49/52 |
| `qwen3_4b-instruct-2507-q4_K_M-tool-20261013T235256.md` | Tryb `tool`, polityka `.3` (iteracja 2: granice HR, język w opisach parametrów, `MAIL_SUBJECT_LANGUAGE=pl`) | **98,1% (94,6–99,4%)** | 50/52 |
| `qwen3_4b-instruct-2507-q4_K_M-tool-20261013T235844.md` | A/B opisu narzędzia: wariant **bez** „Call exactly once.”, 22 przypadki podatne na duplikaty | 21/22, 0 duplikatów (jak wariant z frazą) | — |
| `qwen3.5_4b-tool-20261014T003821.md` | Inny model: `qwen3.5:4b` z `LLM_THINKING=false`, tylko holdout; mediana 36,9 s (3 razy wolniej) | — | 51/52 (98,1%) |

Od iteracji 2 raport ukrywa szczegóły błędów na holdout (`--reveal-holdout` je pokazuje), żeby nie
stroić promptu pod przypadki kontrolne.

## Wcześniejszy zbiór: 58 przypadków

| Plik | Przebieg | Wynik |
|---|---|---|
| `qwen3_4b-instruct-2507-q4_K_M-20261013T105800.md` | Iteracja 0: prompt wyjściowy, zbiór strojeniowy (48) | 95,8% |
| `qwen3_4b-instruct-2507-q4_K_M-20261013T110644.md` | Iteracja 1: po dwóch ogólnych poprawkach, zbiór strojeniowy (48) | 100% |
| `qwen3_4b-instruct-2507-q4_K_M-20261013T110852.md` | Kontrola przeuczenia: zbiór `holdout` (10), uruchomiony raz | 100% |

Wyniki na tym zbiorze są zawyżone: 10 z 58 przypadków pokrywało się z przykładami z promptu.
