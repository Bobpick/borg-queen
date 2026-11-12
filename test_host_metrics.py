import unittest
import server as queen


class HostMetrics(unittest.TestCase):
    def test_sample_has_cpu_mem_gpu_keys(self):
        queen._cpu_prev = None
        queen.sample_host_metrics()  # prime cpu delta
        h = queen.sample_host_metrics()
        self.assertIn("cpu_pct", h)
        self.assertIn("mem", h)
        self.assertIn("gpu", h)
        self.assertIsNotNone(h["mem"].get("total_gb"))
        self.assertGreater(h["mem"]["total_gb"], 1)
        self.assertIn("util_pct", h["gpu"])
        self.assertIn("temp_c", h["gpu"])
        self.assertIsNotNone(h["cpu_temp_c"])
        self.assertGreater(h["cpu_temp_c"], 10)
        self.assertLess(h["cpu_temp_c"], 115)
        if h["gpu"]["temp_c"] is not None:
            self.assertGreater(h["gpu"]["temp_c"], 10)
            self.assertLess(h["gpu"]["temp_c"], 115)


if __name__ == "__main__":
    unittest.main()
