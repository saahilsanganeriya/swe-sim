"""Historical trend page and its chart fragments; all values come from public measurement rows."""
from __future__ import annotations

from swesim.dashboard import charts, pages
from swesim.dashboard.render import h

METRICS = (
    ("pass_rate", "Pass rate as scenarios accrue", "n", "", 1.0),
    ("questions_per_trial", "Questions per trial", "questions_n", "", None),
    ("confirmations_per_trial", "Confirmations per trial", "confirmations_n", "", None),
    ("cost_per_success", "USD per solved attempt", "cost_known_n", " USD", None),
)


def chart_fragments(data: dict) -> dict[str, str]:
    """Stable chart IDs let the browser preserve each chart/table choice during refresh."""
    fragments = {}
    pairs = sorted({(row["condition"], row["brief_profile"]) for row in data["history"]})
    for condition, profile in pairs:
        selected = [row for row in data["history"] if row["condition"] == condition and row["brief_profile"] == profile]
        cohort = [row for row in data["interaction"] if row["condition"] == condition and row["brief_profile"] == profile]
        for metric, label, nkey, unit, maximum in METRICS:
            cid = f"trend-{condition}-{profile}-{metric}"
            title = f"{condition} · {profile}: {label}"
            fragments[cid] = f"<h2>{h(title)}</h2>" + charts.history(selected, metric=metric, chart_id=cid, title=title,
                                                                   sample_key=nkey, unit=unit, ymax=maximum)
        cid = f"trend-{condition}-{profile}-asking"
        title = f"{condition} · {profile}: asked versus did not ask"
        fragments[cid] = f"<h2>{h(title)}</h2>" + charts.history(cohort, metric="pass_rate", chart_id=cid, title=title, ymax=1.0)
    difficulty = charts.hbars([(row["label"], row["n"]) for row in data["difficulty"]], "trend-difficulty", "Difficulty distribution")
    links = "<p class='small'>" + " · ".join(f"<a href='{h(row['evidence_url'])}'>{h(row['label'])}: {row['n']}</a>" for row in data["difficulty"]) + "</p>"
    fragments["trend-difficulty"] = "<h2>Current valid-scenario difficulty</h2>" + difficulty + links
    return fragments


def body(data: dict, current: dict) -> str:
    select = pages._filter_select
    filters = ("<form class='toolbar' action='/trends' method='get'>"
               + select("purpose", ["comparison", "calibration", "demonstration", "screen"], [current["purpose"]], "purpose")
               + select("condition", ["full", "hidden", "interactive"], current.get("condition"), "condition")
               + select("brief_profile", ["natural", "withheld"], current.get("brief_profile"), "brief profile")
               + select("source", ["github-repo", "swebench-annotated", "authored"], current.get("source"), "source")
               + select("tier", ["A", "B", "C", "D", "E"], current.get("tier"), "model tier")
               + select("difficulty_tier", ["informative", "saturated"], current.get("difficulty_tier"), "difficulty")
               + f"<label class='inline'><input type='checkbox' name='include_saturated' value='1'{' checked' if current['include_saturated'] else ''}> include saturated</label>"
               "<button class='button' type='submit'>Apply</button><a class='button' href='/trends'>Reset</a></form>")
    return ("<h1>Trends</h1><p class='lead'>How results change as scenarios accrue. The leaderboard's validation, difficulty and task-sense gates apply. "
            "Each purpose, condition and brief profile remains separate.</p>" + filters
            + f"<p class='note' id='trend-coverage'>{coverage(data)}</p>"
            + "<p class='muted small'>Rates are descriptive, not causal: asking cohorts differ in their tasks and behavior. Questions and confirmations use known observations only. "
            "Cost per solved attempt is unknown until every included attempt has a cost and at least one succeeds. Configurations may differ across the model's admitted attempts; "
            "the evidence rows retain their frozen configuration hashes.</p>"
            + "<div id='trend-charts' data-live-charts>" + "".join(data["charts"].values()) + "</div>")


def coverage(data: dict) -> str:
    return (f"{data['trial_count']} admitted attempts; {data['undated_trials']} without a completion timestamp are excluded from historical curves; "
            f"{data['unknown_questions']} have unknown question counts. Charts and tables refresh together.")
