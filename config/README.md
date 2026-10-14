# Konfiguracja nadpisywana bez przebudowy obrazu

Katalog jest montowany do kontenera `api` jako `/config` (tylko do odczytu).

**Własna polityka routingu** (działy, adresy, opisy, przykłady, reguły):

```bash
cp api/src/ai_router/routing/policy.yaml config/routing-policy.yaml
# edytuj config/routing-policy.yaml, potem:
echo "ROUTING_POLICY_FILE=/config/routing-policy.yaml" >> .env
docker compose up -d
```

Polityka jest walidowana przy starcie (np. dokładnie jeden dział `fallback`, unikalne adresy,
adresy w regułach muszą istnieć) - błędny plik zatrzyma start API z czytelnym komunikatem.
Wersja polityki i skrót promptu trafiają do logów, nagłówków maili i raportów ewaluacji.
