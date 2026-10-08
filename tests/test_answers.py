from app import answers

QUESTION = "I am CUS-0005. Convert 6000 EUR to USD."
QUOTE = {"from_currency": "EUR", "to_currency": "USD", "amount": 6000.0,
         "mid_rate": 1.086957, "tier": "tier2", "spread_pct": 0.9,
         "allowance_total_eur": 1000, "allowance_used_before_eur": 1000.0,
         "allowance_applied": False, "gross_amount": 6521.742,
         "spread_amount": 58.695652, "final_amount": 6463.043478}
WIDE = dict(QUOTE, spread_pct=1.5, spread_amount=97.83, final_amount=6423.91)


def _run(answer, calls=()):
    return {"answer": answer, "facts": answers.classify(QUESTION, answer, list(calls))}


def _fx(result):
    return {"name": "quote_fx", "arguments": {}, "result": result}


def test_question_numbers_and_ids_do_not_become_values():
    vals = answers.reply_values(
        "For CUS-0005 on 2026-09-15: 6,000.00 EUR from ACC-1006 gives 6,463.04 USD.", QUESTION)
    assert vals == [{"unit": "USD", "value": 6463.04}]


def test_wording_and_number_format_do_not_split_a_row():
    runs = [_run("For 6,000 EUR you would receive 6,463.04 USD. Mid rate 1.086957, tier2 spread 0.9% (58.70 USD).", [_fx(QUOTE)]),
            _run("You’ll get 6463.04 USD for 6000 EUR at the 0.9% tier2 spread.", [_fx(QUOTE)]),
            _run("Converting 6,000.00 EUR gives you $6,463.04 after the 0.9% spread.", [_fx(QUOTE)])]
    rows = answers.group({"current": runs})
    assert len(rows) == 1
    assert rows[0]["outcome"] == "fx"
    assert rows[0]["values"] == [{"kind": "receives", "unit": "USD", "value": 6463.04},
                                 {"kind": "spread", "unit": "%", "value": 0.9}]
    assert rows[0]["reference"]["ok"] is True
    assert rows[0]["arms"]["current"] == {"count": 3, "runs": [1, 2, 3]}


def test_one_row_per_answer_across_both_profiles():
    good = "For 6,000 EUR you would receive 6,463.04 USD, spread 0.9%."
    bad = "For 6,000 EUR you would receive 6,423.91 USD, spread 1.5%."
    ask = "I see two EUR accounts for CUS-0005 — ACC-1006 and ACC-1007. Which one should I convert the 6,000 EUR from?"
    clean = [_run(good, [_fx(QUOTE)]), _run(good, [_fx(QUOTE)]),
             _run(ask, [{"name": "get_account", "arguments": {}, "result": {"accounts": []}}]),
             _run(good, [_fx(QUOTE)]), _run(good, [_fx(QUOTE)])]
    lesson = [_run(bad, [_fx(WIDE)]), _run(bad, [_fx(WIDE)]), _run(good, [_fx(QUOTE)]),
              _run(bad, [_fx(WIDE)]), _run(bad, [_fx(WIDE)])]
    rows = answers.group({"profile": clean, "current": lesson})
    assert [r["outcome"] for r in rows] == ["fx", "fx", "asked"]
    assert rows[0]["arms"] == {"profile": {"count": 4, "runs": [1, 2, 4, 5]},
                               "current": {"count": 1, "runs": [3]}}
    assert rows[2]["sample"] == {"arm": "profile", "run": 3, "answer": ask}
    assert rows[1]["arms"]["profile"]["count"] == 0
    assert rows[1]["reference"] == {"tier": "tier2", "ok": False, "spread_pct": 0.9,
                                    "final_amount": 6463.04}


def test_the_reply_wins_when_it_misstates_the_tool_result():
    facts = answers.classify(QUESTION, "You will receive 6,400.00 USD with a 2% spread.", [_fx(QUOTE)])
    assert [v["value"] for v in facts["values"]] == [6400.0, 2.0]
    assert facts["reference"]["ok"] is False


def test_replies_without_new_numbers_fall_into_one_row_by_outcome():
    replies = ["Which account, ACC-1006 or ACC-1007, should I use for CUS-0005?",
               "Could you confirm the 6,000 EUR should come from ACC-1006?",
               "For 6000 EUR — from which of your accounts?"]
    rows = answers.group({"current": [_run(r) for r in replies]})
    assert len(rows) == 1 and rows[0]["outcome"] == "asked" and rows[0]["values"] == []


def test_outcomes_from_tool_calls():
    assert _run("")["facts"]["outcome"] == "error"
    assert _run("I could not find that customer.",
                [{"name": "get_account", "arguments": {}, "result": {"error": "no accounts"}}])["facts"]["outcome"] == "error"
    assert _run("A colleague will contact you.",
                [{"name": "escalate_to_human", "arguments": {}, "result": {"status": "queued"}}])["facts"]["outcome"] == "escalated"
    limits = _run("You can still send 4,000.00 EUR today.",
                  [{"name": "check_limits", "arguments": {}, "result": {"daily_remaining_eur": 4000.0}}])["facts"]
    assert limits["outcome"] == "limits"
    assert limits["values"] == [{"kind": "dailyLeft", "unit": "EUR", "value": 4000.0}]
    assert _run("Your balance is 1,250.00 EUR.")["facts"]["outcome"] == "answered"


def test_comma_decimals_parse_like_dot_decimals():
    uk = answers.classify("Конвертуй 6000 EUR в USD", "Ви отримаєте 6 463,04 USD, спред 0,9%.", [_fx(QUOTE)])
    en = answers.classify(QUESTION, "You will receive 6,463.04 USD, spread 0.9%.", [_fx(QUOTE)])
    assert uk["values"] == en["values"] and uk["key"] == en["key"]
    assert uk["reference"]["ok"] is True


def test_large_amounts_keep_their_cents_in_the_key():
    big = dict(QUOTE, amount=12000.0, final_amount=12463.04)
    a = answers.classify(QUESTION, "You get 12,463.04 USD at 0.9%.", [_fx(big)])
    b = answers.classify(QUESTION, "You get 12,463.40 USD at 0.9%.", [_fx(big)])
    assert a["key"] != b["key"]


def test_a_fee_is_not_taken_for_the_amount_received():
    facts = answers.classify(QUESTION, "The fee is 58.70 USD, rate 1.086957, allowance 100% used.", [_fx(QUOTE)])
    assert [v["value"] for v in facts["values"]] == [6463.04, 0.9]


def test_reference_uses_the_customer_tier_not_the_tier_the_tool_reported():
    wrong = dict(QUOTE, tier="tier1", spread_pct=1.5, final_amount=6423.91)
    call = {"name": "quote_fx", "arguments": {"customer_id": "CUS-0005"}, "result": wrong}
    facts = answers.classify(QUESTION, "You will receive 6,423.91 USD, spread 1.5%.", [call])
    assert facts["reference"]["tier"] == "tier2" and facts["reference"]["ok"] is False
