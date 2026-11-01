"""Exercise successive frames, including pruning, rather than isolated charts."""

import math
import unittest
from unittest.mock import patch

from rctf_monitor import app as m


class ResourceTimelineTests(unittest.TestCase):
    def sampler(self):
        with patch.object(m.time, "monotonic", return_value=240):
            return m.ResourceSampler()

    def frame(self, sampler, now, samples):
        sampler.samples.clear()
        sampler.samples.extend(samples)
        sampler.last_sample = now
        with patch.object(m.time, "monotonic", return_value=now):
            frame = sampler.sample()
        self.assertTrue(all(now - 120 <= row[0] <= now for row in frame["samples"]))
        return frame

    def test_left_bar_survives_pruning_until_it_scrolls_out(self):
        sampler = self.sampler()
        samples = [(120.1, 90.5, 32.8), (121, 10, 20), (239, 7, 15)]
        for now in (240, 240.2, 241.1, 242.4):
            frame = self.frame(sampler, now, samples)
            for column, peak in ((1, 90.5), (2, 32.8)):
                values = m.resource_series(frame, column, 48)
                self.assertEqual(values[0], peak, (now, column))
                self.assertEqual(max(v for v in values if v is not None), peak)
        frame = self.frame(sampler, 242.5, samples)
        self.assertNotIn(90.5, m.resource_series(frame, 1, 48))
        self.assertNotIn(32.8, m.resource_series(frame, 2, 48))

    def test_existing_columns_only_shift_never_change_height(self):
        for width in (8, 45, 49, 51, 81, 100):
            sampler = self.sampler()
            samples = [
                (120 + i * 0.5, float(i % 97), float(i % 41)) for i in range(241)
            ]
            # The real collector has at most 122 one-second samples.
            samples = samples[::2]
            previous = None
            previous_end = None
            for step in range(101):
                now = 240 + step * 0.2
                frame = self.frame(sampler, now, samples)
                end = math.floor(now * width / 120)
                for column in (1, 2):
                    values = m.resource_series(frame, column, width)
                    if previous is not None:
                        shift = end - previous_end
                        self.assertEqual(
                            values[: width - shift],
                            previous[column][shift:],
                            (width, now, column),
                        )
                    if column == 1:
                        current = {1: values}
                    else:
                        current[2] = values
                previous, previous_end = current, end

    def test_fractional_bucket_boundaries_have_consistent_ids(self):
        for width in (45, 49, 51, 81, 100):
            # 240 is an exact boundary for every integer plot width. One cell
            # later its sample must be at the right edge, never one cell left.
            now = 240 + 120 / width + 0.000001
            frame = {"now": now, "samples": [(240, 80, 25)]}
            self.assertEqual(
                m.resource_series(frame, 1, width), [None] * (width - 1) + [80], width
            )

    def test_active_bucket_closes_once_and_uses_maximum(self):
        sampler = self.sampler()
        samples = [(239, 10, 20), (240.1, 95, 30), (241, 5, 40)]
        for now in (241, 242.4):
            values = m.resource_series(self.frame(sampler, now, samples), 1, 48)
            self.assertEqual(values[-1], 10)
            self.assertNotIn(95, values)
        values = m.resource_series(self.frame(sampler, 242.5, samples), 1, 48)
        self.assertEqual(values[-2:], [10, 95])

    def test_peak_and_rendered_bars_stay_in_sync_across_expiry(self):
        sampler = self.sampler()
        samples = [(120.1, 90.5, 32.8), (121, 10, 20), (239, 7, 15)]
        for now in (240, 240.2, 241.5, 242.5):
            frame = self.frame(sampler, now, samples)
            with patch.object(m, "resource_chart", wraps=m.resource_chart) as chart:
                m.render_resources(frame, 114)  # Both plots are 48 cells.
            for call in chart.call_args_list:
                title, _, peak, values, *_ = call.args
                self.assertEqual(peak, max(v for v in values if v is not None))
                self.assertEqual(
                    peak,
                    (90.5 if title == "CPU" else 32.8)
                    if now < 242.5
                    else (7 if title == "CPU" else 15),
                )

    def test_missing_intervals_and_long_pause_do_not_repeat_old_bars(self):
        sampler = self.sampler()
        m.resource_series(self.frame(sampler, 240, [(239, 80, 20)]), 1, 48)
        values = m.resource_series(self.frame(sampler, 247.5, []), 1, 48)
        self.assertEqual(values[-4:], [80, None, None, None])
        self.assertEqual(
            m.resource_series(self.frame(sampler, 400, []), 1, 48), [None] * 48
        )

    def test_resizing_separates_series_and_bounds_cached_memory(self):
        sampler = self.sampler()
        frame = self.frame(sampler, 240, [(239, 80, 25)])
        for width in range(8, 221):
            for column, peak in ((1, 80), (2, 25)):
                values = m.resource_series(frame, column, width)
                self.assertEqual(len(values), width)
                self.assertEqual(max(v for v in values if v is not None), peak)
                self.assertLessEqual(len(frame["plot_cache"]), 32)

    def test_cached_results_cannot_be_mutated_by_callers(self):
        sampler = self.sampler()
        frame = self.frame(sampler, 240, [(239, 80, 25)])
        values = m.resource_series(frame, 1, 48)
        values[-1] = 99
        self.assertEqual(m.resource_series(frame, 1, 48)[-1], 80)

    def test_clock_reset_and_new_sampler_cannot_reuse_future_bars(self):
        sampler = self.sampler()
        m.resource_series(self.frame(sampler, 240, [(239, 80, 25)]), 1, 48)
        self.assertEqual(
            m.resource_series(self.frame(sampler, 239, []), 1, 48), [None] * 48
        )
        self.assertEqual(
            m.resource_series(self.frame(self.sampler(), 240, []), 1, 48), [None] * 48
        )

    def test_cpu_and_memory_scroll_together_at_odd_terminal_widths(self):
        for width in (109, 110, 111, 119, 120, 121, 139, 140, 219, 220):
            sampler = self.sampler()
            samples = [(120 + i, float(i % 97), float(i % 97)) for i in range(121)]
            for step in range(31):
                frame = self.frame(sampler, 240 + step * 0.2, samples)
                with patch.object(m, "resource_chart", wraps=m.resource_chart) as chart:
                    lines = m.render_resources(frame, width)
                cpu, memory = chart.call_args_list
                self.assertEqual(cpu.args[3], memory.args[3], (width, step))
                self.assertEqual(cpu.args[4], memory.args[4], (width, step))
                self.assertTrue(all(m.display_width(line) == width for line in lines))


if __name__ == "__main__":
    unittest.main()
