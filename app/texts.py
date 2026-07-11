"""Тексты сообщений бота (HTML parse mode)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from app.config import MSK, settings

if TYPE_CHECKING:
    from datetime import datetime

    from app.metrics import Analysis, Delta

PROB_DISPLAY_CAP = 0.9995
PROB_DISPLAY_FLOOR = 0.001
PROB_BAR_WIDTH = 10
NOTIFY_PP_THRESHOLD = 0.05

HELP = (
    "<b>Что я умею</b>\n\n"
    "Слежу за конкурсными списками abit.itmo.ru: считаю вероятность "
    "поступления с учётом балла, договоров и динамики притока, присылаю "
    "уведомления при каждом обновлении списка. Поддерживаются любые "
    "направления: бакалавриат и магистратура, контракт и бюджет.\n\n"
    "<b>Команды</b>\n"
    "/track — начать следить за списком (ссылка + ваш код ЕПГУ)\n"
    "/status — детальный разбор: место, конкуренты, вероятность, прогноз\n"
    "/chart — графики динамики\n"
    "/list — мои подписки (вкл/выкл уведомления, удалить)\n"
    "/settings — дайджест места: как часто присылать (по умолчанию раз в 6 ч)\n"
    "/refresh — принудительно обновить данные с сайта\n\n"
    "<i>Код ЕПГУ — «Уникальный код портала Госуслуг», по нему вы значитесь "
    "в конкурсном списке.</i>"
)

START = (
    "Привет! Я слежу за конкурсными списками ИТМО и оцениваю вероятность "
    "поступления в моменте и в прогнозе.\n\n"
    "Начните с команды /track — пришлёте ссылку на свой список и код "
    "ЕПГУ.\n\n" + HELP
)


def fmt_dt(dt: datetime) -> str:
    """Дата-время по Москве, коротко."""
    return dt.astimezone(MSK).strftime("%d.%m %H:%M")


def pct(x: float) -> str:
    """Вероятность в процентах с зажимом крайних значений."""
    if x >= PROB_DISPLAY_CAP:
        return "&gt;99.9%"
    if x < PROB_DISPLAY_FLOOR:
        return "&lt;0.1%"
    return f"{x * 100:.1f}%"


def _signed(n: int, *, invert_good: bool = False) -> str:
    """Дельта со знаком; invert_good: меньше = лучше (позиция в списке)."""
    if n == 0:
        return "без изменений"
    good = (n < 0) if invert_good else (n > 0)
    arrow = " 📈" if good and invert_good else ""
    sign = "+" if n > 0 else "-"
    return f"{sign}{abs(n)}{arrow}"


def _prob_bar(p: float) -> str:
    filled = round(p * PROB_BAR_WIDTH)
    return "█" * filled + "░" * (PROB_BAR_WIDTH - filled)


def _status_header(title: str, url: str, a: Analysis) -> list[str]:
    return [
        f"📊 <b>{title}</b>",
        f"<i>Данные от {fmt_dt(a.update_time)} МСК · <a href='{url}'>список</a></i>",
        "",
        f"Заявлений: <b>{a.total}</b> · мест: <b>{a.places}</b>",
        f"Договоры: оплачено <b>{a.paid}</b>, одобрено {a.approved}, "
        f"согласий {a.agreements}",
    ]


_STATE_RU: dict[str, str] = {
    "paid": "договор оплачен ✅",
    "approved": "договор одобрен",
    "agreement": "согласие подано",
    "none": "договор/согласие не оформлены",
}


def _status_personal(a: Analysis) -> list[str]:
    score_detail = ""
    if a.exam_score is not None:
        score_detail = f" ({a.exam_score:g} + ИД {a.ia_score or 0:g})"
    b = a.ahead
    ahead_total = (a.position or 1) - 1
    return [
        "",
        f"<b>Ваше место: {a.position} из {a.total}</b>",
        f"Балл: <b>{a.score:g}</b>{score_detail} · "
        f"выше {pct(a.percentile or 0)} списка",
        f"Приоритет: {a.priority} · {_STATE_RU[a.my_state]}",
        "",
        f"<b>Выше вас: {ahead_total} чел.</b>",
        f" ├ оплатили договор: {b.paid}",
        f" ├ договор одобрен: {b.approved}",
        f" ├ подали согласие: {b.agreement}",
        f" └ без договора: {b.none} (с приоритетом 1: {b.none_prio1})",
        "",
        f"Ожидаемо реальных конкурентов выше: <b>{a.mu_ahead_now:.0f}</b>",
        f"Эффективная позиция: <b>~{a.eff_position:.0f}</b> из {a.places}",
    ]


def _status_probability(a: Analysis) -> list[str]:
    deadline = settings.enroll_deadline.strftime("%d.%m")
    lines = [
        "",
        "<b>Вероятность поступления</b>",
        f" сейчас:  {_prob_bar(a.p_now)} <b>{pct(a.p_now)}</b>",
        f" прогноз к {deadline}:  {_prob_bar(a.p_base)} <b>{pct(a.p_base)}</b>",
        f" пессимистично: {pct(a.p_pess)} · оптимистично: {pct(a.p_opt)}",
    ]
    if not (a.calib.enough_data or a.influx.enough_data):
        lines.append(
            "<i>Пока по приорам: динамика уточнится после ~12 ч наблюдений.</i>"
        )
    if a.approximate:
        lines.append(
            "<i>Для бюджетных списков оценка приближённая: модель не решает "
            "задачу распределения по приоритетам между программами.</i>"
        )
    return lines


def _status_dynamics(a: Analysis, day: Delta | None) -> list[str]:
    lines: list[str] = []
    if a.influx.enough_data:
        apply_end = settings.apply_deadline.strftime("%d.%m")
        enroll_end = settings.enroll_deadline.strftime("%d.%m")
        lines += [
            "",
            "<b>Динамика</b>",
            f" приток: ~{a.influx.rate_per_day:.0f} заявл./день, "
            f"из них выше вас ~{a.influx.q_ahead * 100:.0f}%",
            f" оплаты: ~{a.influx.paid_rate_per_day:.1f}/день",
            f" прогноз: ~{a.forecast_total:.0f} заявлений к {apply_end}, "
            f"~{a.forecast_paid:.0f} оплат к {enroll_end}",
            f" новых конкурентов выше вас ожидается: ~{a.mu_new_ahead:.0f}",
        ]
    if day is not None:
        pos_part = ""
        if day.d_position is not None:
            pos_part = f", ваше место {_signed(day.d_position, invert_good=True)}"
        lines += [
            "",
            f"За последние ~{day.hours:.0f} ч: заявлений {_signed(day.d_total)}, "
            f"оплат {_signed(day.d_paid)}{pos_part}",
        ]
    lines += ["", f"До дедлайна оплаты: {a.days_left:.0f} дн."]
    return lines


def format_status(title: str, url: str, a: Analysis, day: Delta | None) -> str:
    """Детальный разбор для /status."""
    lines = _status_header(title, url, a)
    if not a.found:
        lines += [
            "",
            "⚠️ Ваш код ЕПГУ не найден в текущем списке. Проверьте код "
            "командой /list или дождитесь появления заявления в списке.",
        ]
        return "\n".join(lines)
    lines += _status_personal(a)
    lines += _status_probability(a)
    lines += _status_dynamics(a, day)
    return "\n".join(lines)


def format_notification(
    title: str, a: Analysis, d: Delta, prev_p_base: float | None
) -> str:
    """Уведомление об обновлении списка."""
    lines = [
        f"🔔 <b>{title}</b> — список обновился ({fmt_dt(a.update_time)})",
        "",
        f"Заявлений: {a.total} ({_signed(d.d_total)})",
        f"Оплачено договоров: {a.paid} ({_signed(d.d_paid)}), "
        f"одобрено: {a.approved} ({_signed(d.d_approved)})",
    ]
    if a.found:
        pos_str = f"Ваше место: <b>{a.position}</b>"
        if d.d_position:
            pos_str += f" ({_signed(d.d_position, invert_good=True)})"
        lines.append(pos_str)
        prob_str = f"Вероятность: <b>{pct(a.p_base)}</b>"
        if prev_p_base is not None:
            diff_pp = (a.p_base - prev_p_base) * 100
            if abs(diff_pp) >= NOTIFY_PP_THRESHOLD:
                sign = "+" if diff_pp > 0 else "-"
                prob_str += f" ({sign}{abs(diff_pp):.1f} п.п.)"
        lines.append(prob_str)
    else:
        lines.append("⚠️ Ваш код не найден в обновлённом списке.")
    lines.append("\nПодробнее: /status · графики: /chart")
    return "\n".join(lines)


def format_place_digest(
    title: str, a: Analysis, window: Delta | None, hours: int
) -> str:
    """Периодический дайджест «ваше место» (не чаще раза в hours часов)."""
    deadline = settings.enroll_deadline.strftime("%d.%m")
    pos_line = f"Место: <b>{a.position}</b> из {a.total}"
    if window is not None and window.d_position is not None:
        pos_line += f" (за ~{window.hours:.0f} ч: "
        pos_line += f"{_signed(window.d_position, invert_good=True)})"
    lines = [
        f"📍 <b>{title}</b> — дайджест места (раз в {hours} ч)",
        "",
        pos_line,
        f"Эффективная позиция: <b>~{a.eff_position:.0f}</b> из {a.places}",
        f"Вероятность: сейчас <b>{pct(a.p_now)}</b> · "
        f"к {deadline}: <b>{pct(a.p_base)}</b>",
    ]
    if window is not None:
        lines.append(
            f"Заявлений: {a.total} ({_signed(window.d_total)}), "
            f"оплат: {a.paid} ({_signed(window.d_paid)})"
        )
    lines += ["", "Подробнее: /status · настроить: /settings"]
    return "\n".join(lines)


SETTINGS_ITEM = (
    "⚙️ <b>{title}</b> · код <code>{code}</code>\n"
    "Дайджест места — не чаще, чем раз в выбранный интервал:"
)
SETTINGS_SAVED = "Дайджест: {label}"
SETTINGS_OFF_LABEL = "выключен"
SETTINGS_NOT_FOUND = "Подписка не найдена"

TRACK_ASK_URL = (
    "Пришлите ссылку на ваш конкурсный список, например:\n"
    "<code>https://abit.itmo.ru/rating/bachelor/contract/2340</code>\n"
    "Подойдёт любое направление: бакалавриат/магистратура, бюджет/контракт."
)
TRACK_BAD_URL = (
    "Не похоже на ссылку конкурсного списка ИТМО. Нужна ссылка вида\n"
    "<code>https://abit.itmo.ru/rating/bachelor/contract/2340</code>\n"
    "Попробуйте ещё раз или /cancel."
)
TRACK_ASK_ID = (
    "Нашёл список: <b>{title}</b>\n"
    "Заявлений: {total}, мест: {places}.\n\n"
    "Теперь пришлите ваш <b>код ЕПГУ</b> (Уникальный код портала Госуслуг), "
    "по которому вы значитесь в списке — только цифры."
)
TRACK_BAD_ID = (
    "Код должен состоять из цифр (4–12 знаков). Попробуйте ещё раз или /cancel."
)
TRACK_NOT_FOUND = (
    "Код <code>{code}</code> не найден в текущем списке ({total} заявлений). "
    "Сохранить подписку всё равно? Буду проверять при каждом обновлении."
)
TRACK_DONE = (
    "✅ Подписка оформлена: <b>{title}</b>, код <code>{code}</code>.\n"
    "{found_line}\n\n"
    "Буду присылать уведомления при обновлениях списка. Сейчас можно "
    "посмотреть /status и /chart."
)
TRACK_SAVED_NOT_FOUND = "Пока вас нет в списке — проверю при каждом обновлении."
CANCELLED = "Отменено."
NO_SUBS = "У вас пока нет подписок. Начните с /track."
NO_DATA = "По «{title}» пока нет данных — попробуйте /refresh."
REFRESH_NEW = "Обновил: {count} нов. снапшот(а). Смотрите /status."
REFRESH_NONE = "Свежих обновлений на сайте нет — данные актуальны."
CHART_WAIT = "Рисую графики…"
CHART_SINGLE_NOTE = "\nПока один снапшот — линии появятся по мере накопления истории."
LIST_HEADER = "Ваши подписки (нажмите, чтобы включить/выключить уведомления):"
