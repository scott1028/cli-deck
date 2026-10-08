import unittest

from cli_deck.registry import Registry, RingBuffer


class RingBufferTest(unittest.TestCase):
    def test_keeps_tail_when_over_limit(self):
        ring = RingBuffer(limit=10)
        ring.append(b"0123456789")
        self.assertEqual(ring.get(), b"0123456789")
        ring.append(b"abc")
        self.assertEqual(ring.get(), b"3456789abc")
        self.assertEqual(len(ring), 10)

    def test_small_appends_accumulate(self):
        ring = RingBuffer(limit=100)
        for ch in b"hello":
            ring.append(bytes([ch]))
        self.assertEqual(ring.get(), b"hello")

    def test_empty_append_is_noop(self):
        ring = RingBuffer(limit=10)
        ring.append(b"")
        self.assertEqual(len(ring), 0)

    def test_default_limit_about_200kb(self):
        ring = RingBuffer()
        ring.append(b"x" * 300_000)
        self.assertEqual(len(ring), 200_000)
        self.assertEqual(ring.get(), b"x" * 200_000)


class RegistryTest(unittest.TestCase):
    def test_create_get_all_remove(self):
        reg = Registry()
        rec = reg.create("bash -i", ["bash", "-i"], "/tmp", "/tmp/x.log")
        self.assertEqual(reg.get(rec.id), rec)
        self.assertEqual(reg.all(), [rec])
        reg.remove(rec.id)
        self.assertIsNone(reg.get(rec.id))
        self.assertEqual(reg.all(), [])

    def test_ids_unique(self):
        reg = Registry()
        ids = {reg.create("n", ["true"], "/tmp").id for _ in range(50)}
        self.assertEqual(len(ids), 50)

    def test_to_public_has_no_ring(self):
        rec = Registry().create("n", ["true"], "/tmp")
        rec.ring.append(b"data")
        public = rec.to_public()
        self.assertNotIn("ring", public)
        self.assertEqual(public["name"], "n")
        self.assertEqual(public["state"], "running")
        self.assertIsNone(public["exit_code"])


if __name__ == "__main__":
    unittest.main()
