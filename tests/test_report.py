#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Тесты расчёта: пограничные случаи из задания и арифметика до копейки.

Запуск:  py -3 -m unittest discover -s tests -v
"""

import sys
import unittest
from datetime import date
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import advance_report as ar  # noqa: E402


DATA = Path(__file__).resolve().parent.parent / "data"
AS_OF = date(2026, 11, 10)


def by_id(results):
    return {res.contract.contract_id: res for res in results}


def lines(res):
    return {ln.work.work_id: ln for ln in res.lines}


class NumbersTest(unittest.TestCase):
    """Фактические числа по данным из задания."""

    @classmethod
    def setUpClass(cls):
        cls.results, cls.warnings, cls.problems = ar.build_report(DATA, AS_OF)
        cls.by_contract = by_id(cls.results)

    def test_contract_totals(self):
        d1 = self.by_contract["D-1"]
        self.assertEqual(d1.active_total, Decimal("500000.00"))
        self.assertEqual(d1.cancelled_total, Decimal("100000.00"))
        self.assertEqual(d1.paid_total, Decimal("315000.00"))
        self.assertEqual(d1.remaining, Decimal("185000.00"))

        d2 = self.by_contract["D-2"]
        self.assertEqual(d2.active_total, Decimal("90000.00"))
        self.assertEqual(d2.paid_total, Decimal("75000.00"))
        self.assertEqual(d2.remaining, Decimal("15000.00"))

    def test_refunded_payment_is_excluded(self):
        """P-2 вернулся 10.09 - на 10.11 его в выплатах нет."""
        d1 = self.by_contract["D-1"]
        self.assertEqual(d1.paid_total, Decimal("315000.00"))
        for ln in d1.lines:
            self.assertNotIn("P-2", ln.source)

    def test_queued_payment_is_not_paid(self):
        """P-4 на 150 000 заведён, но денег не ушло: не в выплатах, показан отдельно."""
        d1 = self.by_contract["D-1"]
        self.assertEqual(d1.queued_total, Decimal("150000.00"))
        self.assertEqual(d1.paid_total, Decimal("315000.00"))

    def test_cancelled_work_has_no_debt(self):
        w13 = lines(self.by_contract["D-1"])["W-13"]
        self.assertEqual(w13.status_at, "cancelled")
        self.assertEqual(w13.remaining, Decimal("0.00"))

    def test_accepted_later_counts_as_not_accepted(self):
        """W-23 принята 11.11, отчёт на 10.11 - на дату не принята."""
        w23 = lines(self.by_contract["D-2"])["W-23"]
        self.assertEqual(w23.status_at, "done_later")
        self.assertEqual(w23.remaining, Decimal("15000.00"))
        self.assertEqual(self.by_contract["D-2"].remaining_accepted, Decimal("0.00"))

    def test_payment_attached_to_work(self):
        """P-3 на 15 000 привязан к W-14 - он в первую очередь её платёж."""
        w14 = lines(self.by_contract["D-1"])["W-14"]
        self.assertEqual(w14.attached, Decimal("15000.00"))
        self.assertEqual(w14.paid, Decimal("100000.00"))
        self.assertIn("P-3", w14.source)

    def test_everything_reconciles(self):
        self.assertEqual(self.problems, [])

    def test_money_adds_up_to_cent(self):
        total_paid = sum((r.paid_total for r in self.results), Decimal("0.00"))
        self.assertEqual(total_paid, Decimal("390000.00"))
        total_remaining = sum((r.remaining for r in self.results), Decimal("0.00"))
        self.assertEqual(total_remaining, Decimal("200000.00"))


class DateSensitivityTest(unittest.TestCase):
    def test_earlier_date_excludes_later_payments(self):
        results, _, _ = ar.build_report(DATA, date(2026, 9, 15))
        d1 = by_id(results)["D-1"]
        # на 15.09 остаётся только аванс P-1: P-2 вернулся 10.09, P-3 оплачен 25.10,
        # P-4 ещё в очереди
        self.assertEqual(d1.paid_total, Decimal("300000.00"))
        # W-13 отменена только 12.10, поэтому на 15.09 в цене работ все 600 000
        self.assertEqual(d1.active_total, Decimal("600000.00"))
        self.assertEqual(d1.remaining, Decimal("300000.00"))

    def test_date_before_acceptance_changes_status(self):
        # W-12 принята 05.10: на 01.10 она ещё не принята, но и не отменена
        early, _, _ = ar.build_report(DATA, date(2026, 10, 1))
        d1 = by_id(early)["D-1"]
        self.assertEqual(lines(d1)["W-12"].status_at, "done_later")
        self.assertNotEqual(lines(d1)["W-12"].status_at, "done")
        # а W-13 отменена 12.10 - на 01.10 она ещё действует
        self.assertEqual(lines(d1)["W-13"].status_at, "planned")


class AllocationModeTest(unittest.TestCase):
    def test_each_mode_reconciles(self):
        for mode in ("accepted-first", "spread"):
            results, _, problems = ar.build_report(DATA, AS_OF, allocation=mode)
            self.assertEqual(problems, [], "режим {}".format(mode))
            for res in results:
                self.assertEqual(
                    res.works_paid + res.works_remaining, res.active_total,
                    "{} в режиме {}".format(res.contract.contract_id, mode))

    def test_spread_keeps_cents(self):
        results, _, _ = ar.build_report(DATA, AS_OF, allocation="spread")
        d1 = by_id(results)["D-1"]
        paid = sum((ln.paid for ln in d1.lines if ln.status_at != "cancelled"), Decimal("0.00"))
        self.assertEqual(paid, Decimal("315000.00"))


class PrimitiveTest(unittest.TestCase):
    def test_parse_money_formats(self):
        self.assertEqual(ar.parse_money("100000.00"), Decimal("100000.00"))
        self.assertEqual(ar.parse_money("100 000,50"), Decimal("100000.50"))
        self.assertEqual(ar.parse_money("1 234 567,89 руб."), Decimal("1234567.89"))
        self.assertIsNone(ar.parse_money(""))

    def test_no_float_rounding(self):
        self.assertEqual(ar.money(Decimal("0.1") + Decimal("0.2")), Decimal("0.30"))
        self.assertEqual(ar.parse_money("0.005"), Decimal("0.01"))

    def test_spread_sums_to_cent(self):
        class L:
            def __init__(self, wid, price):
                self.price = price
                self.work = type("W", (), {"work_id": wid})()

        if hasattr(ar, "spread"):
            parts = ar.spread(Decimal("100.00"), [L("A", Decimal("1")), L("B", Decimal("1")),
                                                  L("C", Decimal("1"))])
            self.assertEqual(sum(parts, Decimal("0.00")), Decimal("100.00"))

    def test_fmt_money(self):
        self.assertEqual(ar.fmt_money(Decimal("1234567.5")).replace("\u00a0", " "), "1 234 567,50")
        self.assertEqual(ar.fmt_money(Decimal("-500")).replace("\u00a0", " "), "-500,00")

    def test_status_from_dates(self):
        work = ar.Work("W", "D", "t", Decimal("1"), "cancelled", None, date(2026, 11, 20))
        self.assertEqual(ar.work_status_at(work, AS_OF, []), "planned")


if __name__ == "__main__":
    unittest.main(verbosity=2)
