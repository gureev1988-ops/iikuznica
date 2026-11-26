"""Export the small public result set for the 12 public work questions."""
import json
from pathlib import Path

from registry import atomic_write


def show(value, digits=4):
    return "нет данных" if value is None else f"{value:.{digits}f}"


def export_report(results):
    def read(name):
        return json.loads((results / name).read_text(encoding="utf-8"))
    retrieval = read("retrieval_summary.json")
    timing, judge, quality = (read(f) for f in ("summary.json", "judge/decisions.json", "answers/summary.json"))
    if not (retrieval["stage"] == "judged" and timing["paired_complete"] == 12
            and judge["complete"] and quality["complete"] and quality["total"] == 24):
        raise ValueError("Only a complete, judged start12 comparison can be exported")
    destination = results.parent / "runs/start12"
    destination.mkdir(parents=True, exist_ok=True)
    lines = ["# Сравнение способов поиска на start12", "",
             "12 рабочих вопросов, одна Gemma, до пяти исходных пунктов на вопрос. "
             "В таблице качества учтён ответ, который получит сотрудник. Содержательную оценку выставил Qwen.", "",
             "| Показатель | Поиск по пунктам | Реестр |", "|---|---:|---:|",
             f"| Все эталонные пункты в пятёрке | {retrieval['baseline_all']}/12 | {retrieval['registry_all']}/12 |"]
    for key, label in (("acceptable", "Ответ прошёл все проверки судьи и ссылок"),
                       ("correct_action", "Верное действие"), ("answers_with_unsupported_or_unclear", "Ответы с неподтверждённым, неверным или неясным указанием"),
                       ("answers_missing_claim_citations", "Ответы с неполными ссылками"),
                       ("answers_with_material_omissions", "Ответы с существенным пропуском"),
                       ("extra_escalations", "Лишние эскалации"),
                       ("shadow_generator_calls", "Теневые вызовы генератора при программной эскалации")):
        counts = [quality["routes"][route][key] for route in ("bm25", "registry")]
        lines.append(f"| {label} | {counts[0]} | {counts[1]} |")
    for metric, title in (("ttft_s", "Первый токен генератора, с"), ("latency_s", "Полный ответ генератора, с"),
                          ("estimated_lookup_plus_generation_s", "Расчётная задержка поиска и генерации, с")):
        for stat, label in (("median", "медиана"), ("p95_nearest_rank", "p95")):
            values = [show(timing[route][metric][stat]) for route in ("bm25", "registry")]
            lines.append(f"| {title}, {label} | {values[0]} | {values[1]} |")
    spend = [show(timing[r]["reported_cost_usd_including_retries"], 6) for r in ("bm25", "registry")]
    lines.append(f"| Известные расходы генератора на все попытки, USD | {spend[0]} | {spend[1]} |")
    grades = quality["grader_usage_including_retries"]["cost"]
    preparation = judge["cost_includes_retries_and_controls"]["cost"]
    if any(timing[r]["attempts_without_reported_cost"] for r in ("bm25", "registry")) or any(
            v["unknown_attempts"] for v in (grades, preparation)):
        lines.append("\nДля части попыток провайдер не вернул расходы. Суммы отражают известную часть.")
    lines.extend(["", f"Фоновая проверка реестра — {judge['jobs_valid']} заданий, "
                  f"{show(preparation['known_total'], 6)} USD известных расходов. Открытых флагов — {len(judge['flags'])}.",
                  f"Оценка готовых ответов — {quality['grader_attempts']} попыток, {show(grades['known_total'], 6)} USD. "
                  "Эти расходы относятся к эксперименту. Расходы первоначальной разметки норм в Codex отдельно не измерялись.",
                  "", "Оба плеча исполняют генерацию даже при споре, чтобы измерить одинаковый вызов. "
                  "При программной эскалации качество считается по показанному сообщению, а полное время и расходы генератора "
                  "в таблице включают теневой вызов. Расчётная задержка складывает два отдельных замера и не включает интерфейс.",
                  "", "Проверь спорные ответы по quality.md и переданным пунктам. Рабочая выборка мала. "
                  "Выбор архитектуры требует независимой проверки после фиксации настроек."])
    atomic_write(destination / "REPORT.md", "\n".join(lines) + "\n")
    for name in ("retrieval_scores.csv", "scores.csv", "summary.json", "answers/quality.md", "answers/summary.json"):
        dest = "answer_quality.json" if name == "answers/summary.json" else Path(name).name
        atomic_write(destination / dest, (results / name).read_text(encoding="utf-8"))
    atomic_write(destination / "judge_flags.json", json.dumps(
        {k: judge[k] for k in ("registry_sha256", "synthetic_controls", "jobs_total", "flags", "cost_includes_retries_and_controls")},
        ensure_ascii=False, indent=2) + "\n")
    print("Result ready for the repository:", destination / "REPORT.md")
