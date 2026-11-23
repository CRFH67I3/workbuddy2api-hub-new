"""免费模型不受低余额保留线拦截。

修复前：免费名单只认目录里的 ``credits: x0.00``。``hy4flash-free`` 这类
上游按名字标了 free、但没进定价目录的模型因此不在名单里；账户余额跌破
保留线后，连它也被 park，网关对所有模型回 503——付费余额低，免费模型
跟着停。

本套钉住三件事：

  - ``model_id_is_free``：``credits`` 是主信号，明确非零价格永远优先；
    只有价格缺失时才使用经过确认的免费名单（不是任意 ``*free`` 名称）。
  - ``free_models_by_realm``：三个来源（内置快照 / 桌面缓存 / 活目录）
    都走同一判定。
  - 账号层：余额低于保留线时，确认的免费模型仍 ``ready``，付费模型、
    名称带 free 但未确认的模型仍被挡。

No network: both remote-catalogue readers are stubbed in every test.
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_TMP = tempfile.mkdtemp(prefix="wb-free-model-")
os.environ["ACCOUNTS_DIR"] = os.path.join(_TMP, "accounts")
os.environ["WB_PROXY_USAGE_DIR"] = _TMP
os.makedirs(os.environ["ACCOUNTS_DIR"], exist_ok=True)

import wb_accounts
import wb_proxy as P


class ModelIdIsFreeTests(unittest.TestCase):
    def test_credits_zero_means_free(self):
        for value in ("x0.00", "x0", "0", "0.00", "  X0.00  "):
            with self.subTest(value=value):
                self.assertTrue(P.model_id_is_free("some-model", {"credits": value}))

    def test_confirmed_free_ids_without_price_are_free(self):
        self.assertTrue(P.model_id_is_free("hy4flash-free", None))
        self.assertTrue(P.model_id_is_free("Hy4Flash-FREE", {}))
        self.assertTrue(P.model_id_is_free(" hy4free ", None))

    def test_nonzero_price_wins_over_a_free_name(self):
        # 明确非零价格 + 名称带 free：付费判定必须优先，保留线不能被绕过。
        self.assertFalse(P.model_id_is_free("hy4flash-free", {"credits": "x0.30"}))
        self.assertFalse(P.model_id_is_free("hy4free", {"credits": "x9.99"}))

    def test_paid_and_unconfirmed_models_are_not_free(self):
        self.assertFalse(P.model_id_is_free("deepseek-v4.1-flash", {"credits": "x1.00"}))
        self.assertFalse(P.model_id_is_free("glm-5.3", None))
        self.assertFalse(P.model_id_is_free("hy4std", {"credits": ""}))
        self.assertFalse(P.model_id_is_free("", None))
        self.assertFalse(P.model_id_is_free(None, None))
        # 任意 *free 名称不等于确认的免费模型
        self.assertFalse(P.model_id_is_free("made-up-free", None))
        self.assertFalse(P.model_id_is_free("hy4flash", {"credits": "x0.30"}))


class FreeModelsByRealmTests(unittest.TestCase):
    def setUp(self):
        P._free_models_cache.update({"at": 0.0, "data": None})
        self._orig_live = (dict(P._models_cache), dict(P._catalog_meta))
        self._orig_cached = P.read_cached_remote_catalog
        P.read_cached_remote_catalog = lambda realm: None

    def tearDown(self):
        P._models_cache.clear()
        P._models_cache.update(self._orig_live[0])
        P._catalog_meta.clear()
        P._catalog_meta.update(self._orig_live[1])
        P.read_cached_remote_catalog = self._orig_cached
        P._free_models_cache.update({"at": 0.0, "data": None})

    def test_confirmed_free_id_without_price_enters_free_set(self):
        P._models_cache["cn"] = {"at": 0.0, "data": [
            ("hy4flash-free", {}),
            ("deepseek-v4.1-flash", {"credits": "x1.00"}),
            ("hy3", {"credits": "x0.00"}),
        ]}
        free = P.free_models_by_realm()["cn"]
        self.assertIn("hy4flash-free", free)
        self.assertIn("hy3", free)
        self.assertNotIn("deepseek-v4.1-flash", free)

    def test_paid_price_in_cached_catalogue_beats_a_free_name(self):
        P._models_cache["cn"] = {"at": 0.0, "data": []}
        P.read_cached_remote_catalog = lambda realm: (
            ["hy4flash-free", "glm-5.3"],
            {"hy4flash-free": {"credits": "x0.30"}, "glm-5.3": {"credits": "x0.79"}},
        ) if realm == "cn" else None
        free = P.free_models_by_realm()["cn"]
        self.assertNotIn("hy4flash-free", free)
        self.assertNotIn("glm-5.3", free)


class ReserveExemptsFreeModelTests(unittest.TestCase):
    def make_account(self, remain, free_models):
        account = wb_accounts.Account.__new__(wb_accounts.Account)
        account.reserve_credits = 30
        account.credits = {"remain": remain, "used": 1}
        account.free_models = frozenset(free_models)
        return account

    def test_free_model_serves_below_reserve(self):
        account = self.make_account(10, ["hy4flash-free"])
        self.assertFalse(account.reserve_blocked("hy4flash-free"))

    def test_confirmed_free_id_serves_without_catalogue_entry(self):
        # hy4flash-free 不在定价目录里：free_models 为空也必须放行
        account = self.make_account(10, [])
        self.assertFalse(account.reserve_blocked("hy4flash-free"))
        self.assertFalse(account.reserve_blocked("hy4free"))

    def test_paid_model_still_blocked_below_reserve(self):
        account = self.make_account(10, ["hy4flash-free"])
        self.assertTrue(account.reserve_blocked("deepseek-v4.1-flash"))

    def test_unconfirmed_free_name_still_blocked_below_reserve(self):
        # 任意 *free 名称不能绕过保留线
        account = self.make_account(10, [])
        self.assertTrue(account.reserve_blocked("made-up-free"))
        self.assertTrue(account.reserve_blocked("paid-model-free"))

    def test_reserve_off_never_blocks(self):
        account = self.make_account(1, [])
        account.reserve_credits = 0
        self.assertFalse(account.reserve_blocked("deepseek-v4.1-flash"))


if __name__ == "__main__":
    unittest.main()
