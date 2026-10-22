"""Generate examples/dashboard-banque.yaml.

    python scripts/generate_dashboard.py \
        --bank-balance sensor.fr76... --bank-transactions sensor.bnp_paribas_transactions_fr76... \
        --second-balance sensor.fr76... --second-transactions sensor.bnp_paribas_transactions_fr76...

Without arguments it writes placeholders (sensor.SOLDE_BNP, …) to replace.
The device prefix of the other entities is "bnp_paribas" (the bank entry's
name); pass --prefix if yours differs.
"""

from __future__ import annotations

import argparse
import unicodedata
from pathlib import Path
from typing import Any

import yaml

# (display name, icon, mushroom colour, chart colour). Must match DEFAULT_CATEGORIES.
CATEGORIES = [
    ("Courses", "mdi:cart", "amber", "#F5A524"),
    ("Restaurants et cafés", "mdi:silverware-fork-knife", "red", "#E5484D"),
    ("Transport", "mdi:car", "indigo", "#3E63DD"),
    ("Logement et énergie", "mdi:home-lightning-bolt", "purple", "#8E4EC6"),
    ("Abonnements et tech", "mdi:repeat", "cyan", "#00A2C7"),
    ("Shopping", "mdi:shopping", "pink", "#D6409F"),
    ("Santé", "mdi:medical-bag", "green", "#30A46C"),
    ("Loisirs et sorties", "mdi:ticket", "deep-orange", "#F76B15"),
    ("Voyages", "mdi:airplane", "teal", "#12A594"),
    ("Impôts et administration", "mdi:bank", "deep-purple", "#6E56CF"),
    ("Espèces", "mdi:cash", "brown", "#A18072"),
    ("Frais bancaires", "mdi:bank-minus", "grey", "#8D8D8D"),
    ("Virements émis", "mdi:bank-transfer-out", "blue", "#5B5BD6"),
    ("Autre", "mdi:help-circle-outline", "grey", "#B4B4B4"),
]
TILE_CATEGORIES = [
    "Courses",
    "Restaurants et cafés",
    "Transport",
    "Shopping",
    "Abonnements et tech",
    "Loisirs et sorties",
    "Logement et énergie",
    "Santé",
    "Autre",
]
EUR = "EVAL:function(v){return Math.round(v)+' €'}"


def slug(text: str) -> str:
    plain = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    out = "".join(c.lower() if c.isalnum() else "_" for c in plain)
    while "__" in out:
        out = out.replace("__", "_")
    return out.strip("_")


class Literal(str):
    """Rendered as a YAML block scalar (|-), needed for markdown/Jinja."""


def _literal(dumper: yaml.Dumper, data: Literal) -> yaml.Node:
    return dumper.represent_scalar("tag:yaml.org,2002:str", data, style="|")


yaml.add_representer(Literal, _literal)


def build(a: argparse.Namespace) -> dict[str, Any]:
    p = a.prefix
    ent = {
        "total": f"sensor.{p}_solde_total",
        "spent": f"sensor.{p}_depenses_totales_du_mois",
        "income": f"sensor.{p}_revenus_du_mois",
        "browse": f"sensor.{p}_operation_selectionnee",
        "over": f"sensor.{p}_budgets_depasses",
        "f_cat": f"select.{p}_filtre_des_operations",
        "f_period": f"select.{p}_periode_des_operations",
        "classify": f"select.{p}_classer_l_operation",
        "prev": f"button.{p}_operation_precedente",
        "next": f"button.{p}_operation_suivante",
    }
    cat_sensor = {name: f"sensor.{p}_depenses_{slug(name)}" for name, *_ in CATEGORIES}
    budget = {name: f"number.{p}_budget_{slug(name)}" for name, *_ in CATEGORIES}
    accounts = [
        ("BNP Paribas", "BNP", a.bank_balance, a.bank_transactions, "green", "#00915A", "🟢"),
        (
            "Hello bank!",
            "Hello",
            a.second_balance,
            a.second_transactions,
            "light-blue",
            "#2D9CDB",
            "🔵",
        ),
    ]
    first_iban = f"state_attr('{a.bank_balance}', 'iban')"

    def money(expr: str) -> str:
        return f"{{{{ '%.2f'|format({expr})|replace('.', ',') }}}} €"

    # ------------------------------------------------------------ lists ---
    ops_list = Literal(
        f"""{{% set e = '{ent["browse"]}' %}}
{{% set bnp = {first_iban} %}}
{{% set ops = state_attr(e, 'operations') or [] %}}
**{{{{ state_attr(e, 'total') or 0 }}}} opérations** · {{{{ state_attr(e, 'filter') }}}} · {{{{ state_attr(e, 'period') }}}}
<br>Sorties **{{{{ '%.2f'|format(state_attr(e, 'sum_out')|float(0))|replace('.', ',') }}}} €** · Entrées **{{{{ '%.2f'|format(state_attr(e, 'sum_in')|float(0))|replace('.', ',') }}}} €**{{% if (state_attr(e, 'total') or 0) > ops|length %}} · {{{{ ops|length }}}} plus récentes affichées{{% endif %}}
{{% set ns = namespace(day='') %}}
{{% for t in ops %}}
{{% if t.date != ns.day %}}{{% set ns.day = t.date %}}

#### {{{{ (t.date or '')[8:10] }}}}/{{{{ (t.date or '')[5:7] }}}}/{{{{ (t.date or '')[0:4] }}}}
{{% endif %}}
<ha-icon icon="{{{{ t.icon }}}}"></ha-icon> **{{{{ t.merchant[:30]|trim }}}}** · **{{{{ '%+.2f'|format(t.amount)|replace('.', ',') }}}} €** · {{{{ t.category_name }}}} · {{{{ 'BNP' if t.account == bnp else 'Hello' }}}}{{% if t.pending %}} · *en attente*{{% endif %}}<br>
<sub>{{{{ t.label }}}}</sub>
{{% endfor %}}"""
    )
    period_tile = {
        "type": "tile",
        "entity": ent["f_period"],
        "name": "Période",
        "features": [{"type": "select-options"}],
        "features_position": "inline",
        "grid_options": {"columns": "full"},
    }

    def drilldown(name: str) -> dict[str, Any]:
        """Tap action: filter the browser on a category, then open the list."""
        return {
            "action": "fire-dom-event",
            "browser_mod": {
                "service": "browser_mod.sequence",
                "data": {
                    "sequence": [
                        {
                            "service": "select.select_option",
                            "data": {"entity_id": ent["f_cat"], "option": name},
                        },
                        {
                            "service": "browser_mod.popup",
                            "data": {
                                "title": name,
                                "size": "wide",
                                "content": {
                                    "type": "vertical-stack",
                                    "cards": [
                                        {
                                            k: v
                                            for k, v in period_tile.items()
                                            if k != "grid_options"
                                        },
                                        {"type": "markdown", "content": ops_list},
                                    ],
                                },
                            },
                        },
                    ]
                },
            },
        }

    # ---------------------------------------------------------- comptes ---
    comptes: list[dict[str, Any]] = [
        {"type": "heading", "heading": "Mes comptes", "icon": "mdi:wallet-outline"},
        {
            "type": "custom:mushroom-template-card",
            "entity": ent["total"],
            "primary": "{% set t = states(entity)|float(0) %}"
            "{{ '{:,.2f}'.format(t).replace(',', ' ').replace('.', ',') }} €",
            "secondary": "{% set n = state_attr(entity, 'overdrawn_accounts')|int(0) %}"
            "Solde total · {{ 'à découvert' if states(entity)|float(0) < 0 else 'créditeur' }}"
            "{% if n %} · {{ n }} compte{{ 's' if n > 1 }} à découvert{% endif %}",
            "icon": "{{ 'mdi:alert-circle-outline' if states(entity)|float(0) < 0 "
            "else 'mdi:piggy-bank-outline' }}",
            "icon_color": "{{ 'red' if states(entity)|float(0) < 0 else 'green' }}",
            "tap_action": {"action": "more-info"},
            "layout": "horizontal",
            "fill_container": True,
            "grid_options": {"columns": "full"},
        },
    ]
    for label, short, bal, tx, color, _hex, _dot in accounts:
        comptes.append(
            {
                "type": "custom:mushroom-template-card",
                "entity": bal,
                "primary": label,
                "secondary": f"{{% set b = states('{bal}')|float(0) %}}"
                f"{money('b')}{{% if b < 0 %}} · À DÉCOUVERT{{% endif %}}"
                f" · {{{{ state_attr('{tx}', 'month_spent')|float(0)|round(0)|int }}}} € ce mois",
                "icon": "mdi:bank" if short == "BNP" else "mdi:bank-outline",
                "icon_color": f"{{{{ 'red' if states('{bal}')|float(0) < 0 else '{color}' }}}}",
                "multiline_secondary": True,
                "tap_action": {"action": "more-info"},
                "grid_options": {"columns": 6},
            }
        )
    comptes.append(
        {
            "type": "custom:apexcharts-card",
            "header": {
                "show": True,
                "title": "Évolution des soldes (30 jours)",
                "show_states": False,
            },
            "graph_span": "30d",
            "apex_config": {
                "chart": {"height": 230},
                "legend": {"position": "top"},
                "stroke": {"curve": "smooth"},
                "fill": {"type": "gradient", "gradient": {"opacityFrom": 0.35, "opacityTo": 0.02}},
                "yaxis": {"labels": {"formatter": EUR}},
                "tooltip": {
                    "y": {
                        "formatter": "EVAL:function(v){return v.toFixed(2).replace('.',',')+' €'}"
                    }
                },
                "annotations": {
                    "yaxis": [
                        {
                            "y": 0,
                            "borderColor": "#E5484D",
                            "strokeDashArray": 4,
                            "label": {
                                "text": "Découvert",
                                "position": "left",
                                "style": {"color": "#fff", "background": "#E5484D"},
                            },
                        }
                    ]
                },
            },
            "series": [
                {
                    "entity": bal,
                    "name": label,
                    "color": hexc,
                    "type": "area",
                    "stroke_width": 3,
                    "group_by": {"func": "last", "duration": "1d"},
                }
                for label, _s, bal, _t, _c, hexc, _d in accounts
            ],
        }
    )

    # -------------------------------------------------------- ce mois-ci ---
    mois: list[dict[str, Any]] = [
        {"type": "heading", "heading": "Ce mois-ci", "icon": "mdi:calendar-month-outline"},
        {
            "type": "custom:mushroom-template-card",
            "entity": ent["spent"],
            "primary": "{{ states(entity)|float(0)|round(0)|int }} €",
            "secondary": "{% set n = states(entity)|float(0) %}"
            "{% set p = state_attr(entity, 'previous_month')|float(0) %}"
            "Dépenses{% if p > 0 %} · {{ '%+d'|format(((n - p) / p * 100)|round(0)|int) }} % vs mois dernier{% endif %}",
            "icon": "mdi:arrow-top-right-thin-circle-outline",
            "icon_color": "red",
            "tap_action": {"action": "more-info"},
            "grid_options": {"columns": 6},
        },
        {
            "type": "custom:mushroom-template-card",
            "entity": ent["income"],
            "primary": "{{ states(entity)|float(0)|round(0)|int }} €",
            "secondary": "Revenus",
            "icon": "mdi:arrow-bottom-left-thin-circle-outline",
            "icon_color": "green",
            "tap_action": {"action": "more-info"},
            "grid_options": {"columns": 6},
        },
        {
            "type": "custom:mushroom-template-card",
            "primary": f"{{% set s = states('{ent['income']}')|float(0) - states('{ent['spent']}')|float(0) %}}"
            "{{ '%+d'|format(s|round(0)|int) }} €",
            "secondary": "Épargne du mois (revenus − dépenses)",
            "icon": "mdi:scale-balance",
            "icon_color": f"{{% set s = states('{ent['income']}')|float(0) - states('{ent['spent']}')|float(0) %}}"
            "{{ 'green' if s >= 0 else 'orange' }}",
            "grid_options": {"columns": "full"},
        },
        {
            "type": "custom:apexcharts-card",
            "chart_type": "donut",
            "header": {"show": True, "title": "Répartition des dépenses", "show_states": False},
            "apex_config": {
                "chart": {"height": 320},
                "legend": {"position": "bottom", "fontSize": "12px"},
                "dataLabels": {"enabled": False},
                "plotOptions": {
                    "pie": {
                        "donut": {
                            "size": "70%",
                            "labels": {
                                "show": True,
                                "total": {
                                    "show": True,
                                    "label": "Total",
                                    "formatter": "EVAL:function(w){return Math.round("
                                    "w.globals.seriesTotals.reduce((a,b)=>a+b,0))+' €'}",
                                },
                                "value": {"formatter": EUR},
                            },
                        }
                    }
                },
            },
            "all_series_config": {"transform": "return Math.max(Number(x) || 0, 0);"},
            "series": [
                {"entity": cat_sensor[name], "name": name.split(" et ")[0], "color": hexc}
                for name, _i, _c, hexc in CATEGORIES
            ],
        },
        {
            "type": "custom:apexcharts-card",
            "header": {"show": True, "title": "Dépenses cumulées par compte", "show_states": False},
            "graph_span": "31d",
            "span": {"start": "month"},
            "apex_config": {
                "chart": {"height": 220},
                "legend": {"position": "top"},
                "yaxis": {"labels": {"formatter": EUR}},
            },
            "series": [
                {
                    "entity": tx,
                    "attribute": "month_spent",
                    "name": label,
                    "color": hexc,
                    "type": "line",
                    "curve": "stepline",
                    "stroke_width": 3,
                }
                for label, _s, _b, tx, _c, hexc, _d in accounts
            ],
        },
    ]

    # -------------------------------------------------------- catégories ---
    cats: list[dict[str, Any]] = [
        {"type": "heading", "heading": "Catégories", "icon": "mdi:shape-outline"},
    ]
    info = {name: (icon, color) for name, icon, color, _h in CATEGORIES}
    for name in TILE_CATEGORIES:
        icon, color = info[name]
        cats.append(
            {
                "type": "custom:mushroom-template-card",
                "entity": cat_sensor[name],
                "primary": name,
                "secondary": "{% set n = states(entity)|float(0) %}"
                "{% set b = state_attr(entity, 'budget') %}"
                "{% set p = state_attr(entity, 'previous_month')|float(0) %}"
                "{{ n|round(0)|int }} €{% if b %} / {{ b|round(0)|int }} €"
                "{% if n > b %} · dépassé{% endif %}"
                "{% elif p > 0 %} · {{ p|round(0)|int }} € le mois dernier{% endif %}",
                "icon": icon,
                "icon_color": "{% set b = state_attr(entity, 'budget') %}"
                f"{{{{ 'red' if b and states(entity)|float(0) > b else '{color}' }}}}",
                "badge_icon": "{% set b = state_attr(entity, 'budget') %}"
                "{{ 'mdi:alert' if b and states(entity)|float(0) > b else '' }}",
                "badge_color": "red",
                "tap_action": drilldown(name),
                "hold_action": {"action": "more-info"},
                "grid_options": {"columns": 6},
            }
        )
    cats.append(
        {
            "type": "custom:apexcharts-card",
            "header": {"show": True, "title": "Dépenses et revenus par mois", "show_states": False},
            "graph_span": "365d",
            "span": {"end": "month"},
            "apex_config": {
                "chart": {"height": 240},
                "legend": {"position": "top"},
                "plotOptions": {"bar": {"borderRadius": 6, "columnWidth": "55%"}},
                "yaxis": {"labels": {"formatter": EUR}},
                "xaxis": {"labels": {"format": "MMM"}},
            },
            "series": [
                {
                    "entity": e,
                    "name": n,
                    "color": c,
                    "type": "column",
                    "statistics": {"type": "state", "period": "month", "align": "start"},
                }
                for e, n, c in (
                    (ent["spent"], "Dépenses", "#E5484D"),
                    (ent["income"], "Revenus", "#30A46C"),
                )
            ],
        }
    )

    # ----------------------------------------------------------- budgets ---
    budget_md = Literal(
        f"""{{% set rows = state_attr('{ent["over"]}', 'budgets') or [] %}}
{{% if not rows %}}
*Aucun budget défini. Renseigne un montant ci-dessous (0 = pas de budget).*
{{% else %}}
| Catégorie | | Dépensé |
|:--|:--|--:|
{{% for r in rows %}}{{% set n = [((r.used_pct|int) / 10)|round(0)|int, 10]|min %}}| {{{{ '🔴' if r.over else ('🟠' if r.used_pct >= 80 else '🟢') }}}} {{{{ r.category }}}} | {{{{ '▰' * n }}}}{{{{ '▱' * (10 - n) }}}} {{{{ r.used_pct }}}} % | {{{{ r.spent|round(0)|int }}}} / {{{{ r.budget|round(0)|int }}}} € |
{{% endfor %}}
{{% endif %}}"""
    )
    budgets: list[dict[str, Any]] = [
        {"type": "heading", "heading": "Budgets du mois", "icon": "mdi:target"},
        {"type": "markdown", "content": budget_md},
        {
            "type": "entities",
            "title": "Définir les budgets",
            "show_header_toggle": False,
            "state_color": False,
            "entities": [{"entity": budget[name], "name": name} for name, *_ in CATEGORIES],
        },
    ]

    # ------------------------------------------------- par compte + liste ---
    per_account = Literal(
        "{% for label, ent, color in ["
        + ", ".join(f"('{label}', '{tx}', '{dot}')" for label, _s, _b, tx, _c, _h, dot in accounts)
        + """] %}
{% set spent = state_attr(ent, 'month_spent')|float(0) %}
{% set cats = state_attr(ent, 'by_category') or {} %}
### {{ color }} {{ label }}

**{{ spent|round(0)|int }} €** dépensés · **{{ state_attr(ent, 'month_received')|float(0)|round(0)|int }} €** reçus

| Catégorie | | Montant |
|:--|:--|--:|
{% for name, value in cats.items() %}{% set pct = (value / spent * 100) if spent > 0 else 0 %}| {{ name }} | {{ '▰' * ((pct / 10)|round(0)|int) }}{{ '▱' * (10 - (pct / 10)|round(0)|int) }} | {{ value|round(0)|int }} € |
{% endfor %}

{% endfor %}"""
    )
    recent = Literal(
        f"""{{% set bnp = {first_iban} %}}
| | Opération | Montant |
|:--|:--|--:|
{{% for t in (state_attr('{ent["spent"]}', 'recent_transactions') or [])[:15] %}}| <ha-icon icon="{{{{ t.icon }}}}"></ha-icon> | **{{{{ t.merchant[:28]|trim }}}}**<br><sub>{{% if t.date %}}{{{{ t.date[8:10] }}}}/{{{{ t.date[5:7] }}}} · {{% endif %}}{{{{ t.category_name }}}} · {{{{ 'BNP' if t.account == bnp else 'Hello' }}}}{{% if t.pending %}} · en attente{{% endif %}}</sub> | {{{{ '%+.2f'|format(t.amount)|replace('.', ',') }}}} € |
{{% endfor %}}"""
    )
    comptes_detail: list[dict[str, Any]] = [
        {"type": "heading", "heading": "Par compte", "icon": "mdi:compare-horizontal"},
        {"type": "markdown", "content": per_account},
        {"type": "heading", "heading": "Dernières opérations", "icon": "mdi:format-list-bulleted"},
        {"type": "markdown", "content": recent},
    ]

    # ------------------------------------------------- vue opérations ---
    detail = Literal(
        f"""{{% set e = '{ent["browse"]}' %}}
{{% set a = state_attr(e, 'amount') %}}
{{% if a is none %}}
*Aucune opération pour ce filtre.*
{{% else %}}
{{% set d = state_attr(e, 'date') or '' %}}
{{% set bd = (state_attr(e, 'detail') or {{}}).get('booking_date') or '' %}}
<center>

<ha-icon icon="{{{{ (state_attr(e, 'detail') or {{}}).get('icon', 'mdi:receipt') }}}}"></ha-icon>

## {{{{ '%+.2f'|format(a)|replace('.', ',') }}}} €
**{{{{ states(e) }}}}**

{{{{ state_attr(e, 'category_name') }}}} · {{{{ 'BNP Paribas' if state_attr(e, 'account') == {first_iban} else 'Hello bank!' }}}}

</center>

| | |
|:--|--:|
| Date d'achat | {{{{ d[8:10] }}}}/{{{{ d[5:7] }}}}/{{{{ d[0:4] }}}} |
| Date de comptabilisation | {{{{ bd[8:10] }}}}/{{{{ bd[5:7] }}}}/{{{{ bd[0:4] }}}} |
| Statut | {{{{ 'En attente' if state_attr(e, 'pending') else 'Comptabilisée' }}}} |
| Position | {{{{ state_attr(e, 'position') }}}} / {{{{ state_attr(e, 'total') }}}} |

**Libellé exact**
```
{{{{ state_attr(e, 'label') }}}}
```
{{% endif %}}"""
    )

    def nav(label: str, icon: str, button: str) -> dict[str, Any]:
        return {
            "type": "custom:mushroom-template-card",
            "primary": label,
            "icon": icon,
            "icon_color": "blue",
            "layout": "vertical",
            "tap_action": {
                "action": "perform-action",
                "perform_action": "button.press",
                "target": {"entity_id": button},
            },
            "grid_options": {"columns": 6},
        }

    operations_view = {
        "title": "Opérations",
        "path": "operations",
        "icon": "mdi:receipt-text-outline",
        "type": "sections",
        "max_columns": 2,
        "sections": [
            {
                "type": "grid",
                "cards": [
                    {"type": "heading", "heading": "Opération", "icon": "mdi:magnify"},
                    {
                        "type": "tile",
                        "entity": ent["f_cat"],
                        "name": "Catégorie",
                        "features": [{"type": "select-options"}],
                        "grid_options": {"columns": 6},
                    },
                    {**period_tile, "features_position": "bottom", "grid_options": {"columns": 6}},
                    {"type": "markdown", "content": detail, "grid_options": {"columns": "full"}},
                    nav("Plus récente", "mdi:chevron-left", ent["prev"]),
                    nav("Plus ancienne", "mdi:chevron-right", ent["next"]),
                    {
                        "type": "tile",
                        "entity": ent["classify"],
                        "name": "Classer ce marchand dans…",
                        "icon": "mdi:tag-arrow-right-outline",
                        "features": [{"type": "select-options"}],
                        "grid_options": {"columns": "full"},
                    },
                ],
            },
            {
                "type": "grid",
                "cards": [
                    {"type": "heading", "heading": "Liste", "icon": "mdi:format-list-bulleted"},
                    {"type": "markdown", "content": ops_list},
                ],
            },
        ],
    }

    return {
        "title": "Ma banque",
        "views": [
            {
                "title": "Ma banque",
                "path": "banque",
                "icon": "mdi:bank",
                "type": "sections",
                "max_columns": 3,
                "sections": [
                    {"type": "grid", "cards": comptes},
                    {"type": "grid", "cards": mois},
                    {"type": "grid", "cards": cats},
                    {"type": "grid", "cards": budgets},
                    {"type": "grid", "cards": comptes_detail},
                ],
            },
            operations_view,
        ],
    }


HEADER = """\
# Tableau de bord "Ma banque" pour l'intégration Enable Banking (0.10.0+).
# Généré par scripts/generate_dashboard.py.
#
# Cartes nécessaires (HACS > Dashboard) : Mushroom, ApexCharts Card.
# Intégration nécessaire pour le détail d'une catégorie au clic : Browser Mod.
#
# Remplace les 4 entités ci-dessous si elles sont encore en majuscules :
#   sensor.SOLDE_BNP / sensor.SOLDE_HELLO               -> capteurs de solde (IBAN)
#   sensor.TRANSACTIONS_BNP / sensor.TRANSACTIONS_HELLO -> capteurs "Transactions …"
#
"""


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prefix", default="bnp_paribas")
    parser.add_argument("--bank-balance", default="sensor.SOLDE_BNP")
    parser.add_argument("--bank-transactions", default="sensor.TRANSACTIONS_BNP")
    parser.add_argument("--second-balance", default="sensor.SOLDE_HELLO")
    parser.add_argument("--second-transactions", default="sensor.TRANSACTIONS_HELLO")
    parser.add_argument("--out", default="examples/dashboard-banque.yaml")
    args = parser.parse_args()
    text = yaml.dump(
        build(args), allow_unicode=True, sort_keys=False, width=1000, default_flow_style=False
    )
    Path(args.out).write_text(HEADER + text, encoding="utf-8")


if __name__ == "__main__":
    main()
