import pytest

from .run_usage import cost_usd, usage_by_window

PRICES = {"decision_model": {"input_usd_per_mtok": .4, "cached_input_usd_per_mtok": .1, "output_usd_per_mtok": 1.6},
          "optimizer": {"input_usd_per_mtok": 1., "output_usd_per_mtok": 0.}}


def cycle(number, start, end, *calls):
    return [{"kind": "cycle-started", "cycle_number": number, "created_at": start}, *calls,
            {"kind": "cycle-completed", "cycle_number": number, "created_at": end}]


def call(kind, number, prompt, output, cached=0):
    return {"kind": kind, "cycle_number": number,
            "usage": {"prompt_tokens": prompt, "completion_tokens": output,
                      "prompt_tokens_details": {"cached_tokens": cached}}}


def test_usage_is_counted_per_window_of_cycles_and_in_total_at_list_prices():
    events = [*cycle(1, "2026-10-09T10:00:00+00:00", "2026-10-09T10:00:02+00:00",
                     call("decision-response", 1, 1000, 20, cached=400)),
              *cycle(2, "2026-10-09T10:00:03+00:00", "2026-10-09T10:00:10+00:00",
                     call("decision-response", 2, 1000, 20), call("optimizer-response", 2, 5000, 300)),
              *cycle(3, "2026-10-09T10:00:11+00:00", "2026-10-09T10:00:12+00:00",
                     call("decision-response", 3, 500, 10)),
              call("optimizer-response", None, 100, 0)]
    usage = usage_by_window(events, PRICES, window=2)
    first, second = usage["windows"]
    assert (first["first_cycle"], first["last_cycle"], second["first_cycle"], second["last_cycle"]) == (1, 2, 3, 3)
    assert first["decision_model"] == {"requests": 2, "input_tokens": 2000, "cached_input_tokens": 400,
                                       "output_tokens": 40, "usd": pytest.approx((1600 * .4 + 400 * .1 + 40 * 1.6) / 1e6)}
    assert first["optimizer"]["usd"] == pytest.approx(5000 / 1e6)
    assert first["wall_clock_seconds"] == 10
    assert usage["total"]["optimizer"]["requests"] == 2
    assert usage["total"]["decision_model"]["input_tokens"] == 2500
    assert usage["total"]["usd"] == pytest.approx(sum(w["usd"] for w in usage["windows"]) + 100 / 1e6)
    assert usage["total"]["wall_clock_seconds"] == 12


def test_a_cached_rate_defaults_to_the_input_rate_and_prices_must_cover_both_models():
    assert cost_usd({"input_tokens": 10, "cached_input_tokens": 10, "output_tokens": 0},
                    {"input_usd_per_mtok": 1., "output_usd_per_mtok": 0.}) == pytest.approx(1e-5)
    with pytest.raises(ValueError):
        usage_by_window([], {"decision_model": PRICES["decision_model"]})
