"""Routing policy: the single source of truth about departments.

`Department` values are the exact target e-mail addresses. The enum validates the `department`
argument of the agent's `send_email` tool; the JSON Schema shown to the model is a plain
`{"type": "string", "enum": [...]}` (see `routing/agent.py` for why). Detailed rules live in
`DEPARTMENT_POLICIES` and are rendered into the agent instructions and the OpenAPI description.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType


class Department(StrEnum):
    """E-mail address of the department responsible for the request."""

    HUMAN_RESOURCES = "human-resources@example.com"
    """HR / people & culture: recruitment, onboarding, training, benefits, conflicts."""

    HELP_DESK = "help-desk@example.com"
    """First-line support for ONE user's device, account or software problem."""

    IT = "it@example.com"
    """IT infrastructure: outages affecting many users, security incidents, system access."""

    KADRY = "kadry@example.com"
    """Personnel administration & payroll: leave, sick leave, working time, salary, contracts."""

    OTHER = "other@example.com"
    """Unclear, unrelated, empty or nonsense messages (fallback)."""


@dataclass(frozen=True, slots=True)
class DepartmentPolicy:
    """Human- and model-readable description of one department."""

    name: str
    name_pl: str
    scope: str
    not_here: str
    keywords_pl: tuple[str, ...]
    examples: tuple[str, ...]


DEPARTMENT_POLICIES: Mapping[Department, DepartmentPolicy] = MappingProxyType(
    {
        Department.KADRY: DepartmentPolicy(
            name="Personnel administration & payroll",
            name_pl="Kadry i płace",
            scope=(
                "leave requests and absences, sick leave, working time and overtime, salary, "
                "payslips and payroll, PIT-11, employment contracts and annexes, employment "
                "certificates, changes of personal or bank account data"
            ),
            not_here="recruitment, training, benefits, workplace conflicts -> human-resources",
            keywords_pl=(
                "urlop",
                "L4",
                "zwolnienie lekarskie",
                "wypłata",
                "pensja",
                "pasek płacowy",
                "umowa",
                "zaświadczenie",
                "nadgodziny",
            ),
            examples=(
                "Chciałbym zgłosić urlop na jutro",
                "Kiedy przyjdzie wypłata za ten miesiąc?",
            ),
        ),
        Department.HUMAN_RESOURCES: DepartmentPolicy(
            name="HR / People & Culture",
            name_pl="HR",
            scope=(
                "recruitment and job offers, onboarding, training and development, benefits "
                "(sports card, private medical care), performance reviews, career paths, "
                "workplace conflicts, mobbing or harassment reports, wellbeing, team events"
            ),
            not_here="administrative and payroll formalities -> kadry",
            keywords_pl=(
                "rekrutacja",
                "szkolenie",
                "benefit",
                "karta sportowa",
                "awans",
                "konflikt",
                "mobbing",
                "onboarding",
            ),
            examples=(
                "Jakie szkolenia są dostępne w tym kwartale?",
                "Kierownik regularnie na mnie krzyczy przy całym zespole",
            ),
        ),
        Department.HELP_DESK: DepartmentPolicy(
            name="First-line IT support (single user)",
            name_pl="Help desk",
            scope=(
                "a problem of ONE person: my computer, laptop, monitor, printer or phone does not "
                "work, cannot log in, password reset, locked account, e-mail/Outlook/Teams/Office "
                "issues, installing software on my computer, requests for equipment or accessories"
            ),
            not_here=(
                "outages affecting many users, security incidents, access rights to systems -> it"
            ),
            keywords_pl=(
                "komputer",
                "laptop",
                "drukarka",
                "hasło",
                "nie mogę się zalogować",
                "Outlook",
                "mysz",
                "monitor",
            ),
            examples=(
                "Nie działa mi komputer",
                "Zapomniałem hasła do poczty",
            ),
        ),
        Department.IT: DepartmentPolicy(
            name="IT department (infrastructure and systems)",
            name_pl="Dział IT",
            scope=(
                "outages affecting many people or services (servers, network, office Wi-Fi, VPN "
                "for everyone), security incidents (phishing, malware, data leak), access rights "
                "to systems and databases, new systems and integrations, licences, IT projects"
            ),
            not_here="an individual user's problem with their own device -> help-desk",
            keywords_pl=(
                "serwer",
                "sieć",
                "awaria systemu",
                "baza danych",
                "phishing",
                "wirus",
                "uprawnienia",
                "VPN",
                "wdrożenie",
            ),
            examples=(
                "Serwer z bazą danych nie odpowiada, cały dział nie może pracować",
                "Dostałem podejrzanego maila z prośbą o podanie hasła",
            ),
        ),
        Department.OTHER: DepartmentPolicy(
            name="Other / fallback",
            name_pl="Pozostałe",
            scope=(
                "unclear, empty, nonsense or spam messages, topics unrelated to the departments "
                "above (office administration, parking, catering, sales, legal) and messages "
                "whose department cannot be determined with reasonable confidence"
            ),
            not_here="anything that clearly matches one of the departments above",
            keywords_pl=(),
            examples=(
                "Gdzie mogę zaparkować rower?",
                "asdfgh",
            ),
        ),
    }
)
