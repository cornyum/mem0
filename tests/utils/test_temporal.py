"""Timezone-aware temporal helpers and Chinese prompt conditioning."""

from datetime import datetime, timedelta, timezone

import pytest

from mem0.configs.prompts import generate_additive_extraction_prompt
from mem0.utils.temporal import (
    coerce_observation_datetime,
    messages_contain_cjk,
    observation_date_text,
    resolve_timezone,
    timezone_label,
    weekday_name,
)


def test_resolve_timezone_defaults_to_system_local():
    tz = resolve_timezone(None)
    assert tz is not None
    # Explicit aliases round-trip to the same object contract (a tzinfo).
    assert resolve_timezone("system") is not None
    assert resolve_timezone(tz) is tz


def test_resolve_timezone_accepts_iana_offsets_and_utc():
    shanghai = resolve_timezone("Asia/Shanghai")
    assert observation_date_text("2026-08-16T20:30:00+00:00", shanghai) == "2026-08-17"
    assert resolve_timezone("UTC") is timezone.utc
    offset = resolve_timezone("-05:00")
    assert offset.utcoffset(None) == timedelta(hours=-5)
    assert "Asia/Shanghai" in timezone_label(shanghai)
    assert "+08:00" in timezone_label(shanghai)


def test_resolve_timezone_rejects_invalid_explicit_values():
    with pytest.raises(ValueError):
        resolve_timezone("Not/AZone")
    with pytest.raises(ValueError):
        resolve_timezone("")
    with pytest.raises(ValueError):
        resolve_timezone(123)


def test_epoch_boundary_uses_requested_timezone():
    # 2026-08-16 20:30 UTC is already 2026-08-17 in Shanghai.
    epoch = int(datetime(2026, 8, 16, 20, 30, tzinfo=timezone.utc).timestamp())
    assert observation_date_text(epoch, resolve_timezone("UTC")) == "2026-08-16"
    assert observation_date_text(epoch, resolve_timezone("Asia/Shanghai")) == "2026-08-17"
    assert observation_date_text(epoch, resolve_timezone("America/New_York")) == "2026-08-16"


def test_weekday_names_are_calendar_consistent():
    day = datetime(2026, 8, 12).date()  # Wednesday
    assert weekday_name(day, "en") == "Wednesday"
    assert weekday_name(day, "zh") == "星期三"


def test_prompt_includes_chinese_weekday_and_fewshot_when_cjk():
    prompt = generate_additive_extraction_prompt(
        new_messages=[{"role": "user", "content": "上周三 7 点我去机场接了我妈妈。"}],
        timestamp="2026-08-16",
        timezone="Asia/Shanghai",
        use_input_language=True,
    )
    assert "2026-08-16 (星期日)" in prompt
    assert "Asia/Shanghai" in prompt
    assert "中文时间表达式处理示例" in prompt
    assert "上周三 7 点我去机场接了我妈妈" in prompt
    assert "2026年8月5日（星期三）" in prompt


def test_prompt_skips_chinese_fewshot_for_english_input():
    prompt = generate_additive_extraction_prompt(
        new_messages=[{"role": "user", "content": "I ran a marathon last week."}],
        timestamp="2023-05-08",
        timezone="UTC",
        use_input_language=True,
    )
    assert "中文时间表达式处理示例" not in prompt
    assert "2023-05-08 (Monday)" in prompt
    assert "UTC" in prompt


def test_prompt_rejects_invalid_timezone():
    with pytest.raises(ValueError):
        generate_additive_extraction_prompt(
            new_messages=[{"role": "user", "content": "hello"}],
            timestamp="2026-08-16",
            timezone="Mars/Olympus",
        )


def test_cjk_detection():
    assert messages_contain_cjk("中文")
    assert messages_contain_cjk([{"role": "user", "content": "中文"}])
    assert not messages_contain_cjk("english only")
    assert not messages_contain_cjk([{"role": "user", "content": "hello"}])


def test_coerce_observation_datetime_accepts_named_english_dates():
    tz = resolve_timezone("Asia/Shanghai")
    parsed = coerce_observation_datetime("1:56 pm on 8 May, 2023", tz)
    assert parsed.date().isoformat() == "2023-05-08"
    assert parsed.tzinfo is tz
