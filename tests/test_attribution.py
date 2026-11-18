"""冻结发布差异归因的端到端验收。

场景覆盖任务书的每一条要求：
- 撤回稿留在历史证据中，但从当前合格集合退出（member_retracted → exited）；
- 稿件更正使旧编码失效、产生新修订（member_corrected）；
- 后到媒体转载在当前视图合并进既有故事（member_merged），且不改写冻结成员关系；
- 第三人裁定改变同一故事的倾向（member_recoded），同一故事上的多种变化只给一个净效应；
- 发布后新事件只进当前视图（entered_after_release），原报告字节不变；
- 采集窗口因撤回/新增/更正而漂移，在 coverage 块单独标注，不冒充倾向变化；
- 所有净值通过 reconciliation 与两版报告实际差值对账。
"""
import unittest
from tests.common import ServiceCase
from meridian.demo import observation, source
from meridian.errors import MeridianError


def both_region_query():
    return {"start": "2026-11-01T00:00:00+00:00", "end": "2026-12-01T00:00:00+00:00",
            "regions": ["North", "South"], "topics": [], "audiences": []}


def coded(service, external_id, *, stance="positive", topic="technology",
          audience="general", reviewers=("alice", "bob"), origin="local-fixture", revision=1):
    for reviewer in reviewers:
        service.coding.submit({"decision_id": f"{external_id}-{reviewer}-r{revision}",
                               "origin": origin, "external_id": external_id, "revision": revision,
                               "reviewer": reviewer, "topic": topic, "stance": stance,
                               "audience": audience,
                               "reason": f"reviewer {reviewer} judgment on revision {revision}"})


class ReleaseAttributionTests(ServiceCase):
    def build_frozen_release(self):
        service = self.service
        spec = both_region_query()
        service.frames.open("f-north-a", "north-a", spec["start"], spec["end"], 2)
        service.frames.open("f-north-b", "north-b", spec["start"], spec["end"], 1)
        service.frames.open("f-south", "south-a", spec["start"], spec["end"], 1)
        service.observations.ingest(observation("a", "north-a", "story-1"))
        service.observations.ingest(observation("c", "north-a", "story-3"))
        service.observations.ingest(observation("b", "north-b", "story-1"))
        service.observations.ingest(observation("d", "south-a", "story-4"))
        for external_id in ("a", "b", "c", "d"):
            coded(service, external_id)
        service.frames.close("f-north-a")
        service.frames.close("f-north-b")
        service.frames.close("f-south")
        preview = service.releases.preview(spec)
        self.assertTrue(preview["quality"]["passed"])
        service.releases.publish("november-2026", spec, preview["token"])

    def post_release_changes(self):
        service = self.service
        # 1) 来源撤回：c 被撤回，story-3 在当前视图消失，但修订台账保留。
        service.observations.retract("local-fixture", "c", "source issued correction and withdrew",
                                     expected_revision=1)
        # 2) 稿件更正：d 发布修订 2，旧编码失效，新编码分歧后由第三人裁定为 negative。
        corrected = observation("d", "south-a", "story-4") | {"headline": "Corrected claims"}
        service.observations.ingest(corrected, expected_revision=1)
        coded(service, "d", stance="positive", revision=2, reviewers=("alice",))
        coded(service, "d", stance="neutral", revision=2, reviewers=("bob",))
        service.coding.adjudicate(
            {"origin": "local-fixture", "external_id": "d", "revision": 2,
             "adjudicator": "carol", "topic": "technology", "stance": "negative",
             "audience": "general", "reason": "ruling on corrected revision: evidence supports negative"})
        # 3) 后到转载：另一媒体在发布后才入库，当前视图并入 story-1，不触碰冻结成员关系。
        service.observations.ingest(
            observation("b2", "north-b", "story-1", "2026-11-05T09:00:00+00:00"))
        coded(service, "b2")
        # 4) 发布后的全新事件：只影响当前视图。
        service.observations.ingest(
            observation("e", "north-a", "story-5", "2026-11-08T09:00:00+00:00"))
        coded(service, "e")

    def test_full_attribution_reconciles_and_explains_every_change(self):
        service = self.service
        self.build_frozen_release()
        frozen_before = service.releases.describe("november-2026")["report"]
        self.post_release_changes()
        frozen_after = service.releases.describe("november-2026")["report"]
        # 原报告与证据解释完全冻结，发布后的任何事件都不能改写它。
        self.assertEqual(frozen_before, frozen_after)
        self.assertEqual(frozen_after["story_count"], 3)

        result = service.attribution.attribute("november-2026")
        self.assertTrue(result["reconciliation"]["passed"], result["reconciliation"]["checks"])
        for check in result["reconciliation"]["checks"]:
            self.assertTrue(check["passed"], check)

        by_story = {(item["region"], item["story_key"]): item
                    for item in result["story_attributions"]}

        # story-3：撤回导致整故事（及其报告故事行）退出，净值 -1，撤回稿仍可在血缘中追溯。
        exited = by_story[("North", "story-3")]
        self.assertEqual(exited["classification"], "exited")
        self.assertIn("member_retracted", exited["reasons"])
        self.assertEqual(exited["net_effect"]["story_row_delta"], -1)
        self.assertEqual(exited["net_effect"]["observation_count_delta"], -1)
        self.assertEqual(exited["net_effect"]["stance_count_delta"], {"positive": -1})
        self.assertEqual(exited["frozen"]["members"][0]["revision"], 1)
        self.assertEqual(exited["current"]["present"], False)
        self.assertEqual([t["change"] for t in exited["bucket_transitions"]], ["bucket_removed"])

        # story-4：同一故事上更正+裁定同时发生，只有一个净效应（不拆成两项差值相加）。
        revised = by_story[("South", "story-4")]
        self.assertEqual(revised["classification"], "revised")
        self.assertIn("member_corrected", revised["reasons"])
        self.assertIn("member_recoded", revised["reasons"])
        self.assertEqual(len(revised["member_changes"]), 1)
        change = revised["member_changes"][0]
        self.assertEqual(change["before"]["revision"], 1)
        self.assertEqual(change["after"]["revision"], 2)
        self.assertEqual(change["before"]["stance"], "positive")
        self.assertEqual(change["after"]["stance"], "negative")
        self.assertEqual(revised["net_effect"]["story_row_delta"], 0)
        self.assertEqual(revised["net_effect"]["stance_count_delta"],
                         {"negative": 1, "positive": -1})
        # 桶行未增减，只是同一报告故事行的倾向 positive→negative。
        self.assertEqual([t["change"] for t in revised["bucket_transitions"]], ["bucket_revised"])
        self.assertEqual(revised["bucket_transitions"][0]["story_stance_delta"],
                         {"negative": 1, "positive": -1})

        # story-1：后到转载在当前侧合并，冻结侧仍只有发布时的两名成员。
        merged = by_story[("North", "story-1")]
        self.assertEqual(merged["classification"], "revised")
        self.assertIn("member_merged", merged["reasons"])
        self.assertEqual(len(merged["frozen"]["members"]), 2)
        self.assertEqual(len(merged["current"]["members"]), 3)
        self.assertEqual(merged["net_effect"]["story_row_delta"], 0)
        self.assertEqual(merged["net_effect"]["observation_count_delta"], 1)
        current_origins = {(m["origin"], m["external_id"]) for m in merged["current"]["members"]}
        self.assertIn(("local-fixture", "b2"), current_origins)
        frozen_origins = {(m["origin"], m["external_id"]) for m in merged["frozen"]["members"]}
        self.assertNotIn(("local-fixture", "b2"), frozen_origins)

        # story-5：发布后新事件，只在当前视图。
        entered = by_story[("North", "story-5")]
        self.assertEqual(entered["classification"], "entered")
        self.assertEqual(entered["frozen"]["present"], False)
        self.assertEqual(entered["net_effect"]["story_row_delta"], 1)
        self.assertEqual([t["change"] for t in entered["bucket_transitions"]], ["bucket_added"])

        # 汇总分母：冻结 3 个故事、当前 3 个（-1 退出 +1 进入）；窗口观察 4→6
        # （d 的更正是新头修订 +1，b2 与 e 各 +1；撤回稿仍留在窗口分母中）。
        totals = result["totals"]
        self.assertEqual(totals["frozen"]["story_count"], 3)
        self.assertEqual(totals["current"]["story_count"], 3)
        self.assertEqual(totals["deltas"]["story_count"], 0)
        self.assertEqual(totals["frozen"]["observations_in_window"], 4)
        self.assertEqual(totals["current"]["observations_in_window"], 6)
        self.assertEqual(totals["deltas"]["observations_in_window"], 2)
        self.assertEqual(totals["current"]["excluded"]["retracted"], 1)

        # 地区行保留两侧完整分母，并给出可直接对账的倾向计数差。
        north = next(row for row in result["regions"] if row["region"] == "North")
        self.assertEqual(north["frozen_story_denominator"], 2)
        self.assertEqual(north["current_story_denominator"], 2)
        self.assertEqual(north["story_count_delta"], 0)
        self.assertEqual(north["stances"]["positive"]["frozen_count"], 2)
        self.assertEqual(north["stances"]["positive"]["current_count"], 2)
        self.assertEqual(north["stances"]["positive"]["count_delta"], 0)
        south = next(row for row in result["regions"] if row["region"] == "South")
        self.assertEqual(south["frozen_story_denominator"], 1)
        self.assertEqual(south["current_story_denominator"], 1)
        self.assertEqual(south["stances"]["positive"]["count_delta"], -1)
        self.assertEqual(south["stances"]["negative"]["count_delta"], 1)
        # 份额变化注明混合了分母效应，不能当作纯粹的倾向漂移。
        self.assertIn("denominator", south["stances"]["positive"]["share_delta_note"])

        # 来源维度：north-a 撤回 -1 与新增 +1 抵消为净 0（不能把两项分开报成两个净值）；
        # north-b 因后到转载 +1。故事级成员清单仍保留两个方向各自的证据。
        source_delta = {row["source_id"]: row for row in north["source_eligible_delta"]}
        self.assertEqual(source_delta["north-a"]["frozen_eligible_observations"], 2)
        self.assertEqual(source_delta["north-a"]["current_eligible_observations"], 2)
        self.assertEqual(source_delta["north-a"]["eligible_delta"], 0)
        self.assertEqual(source_delta["north-b"]["eligible_delta"], 1)
        # 抵消的两个方向分别可在故事归因中复核，而不是被净值掩盖。
        exited_members = by_story[("North", "story-3")]["frozen"]["members"]
        entered_members = by_story[("North", "story-5")]["current"]["members"]
        self.assertEqual([m["source_id"] for m in exited_members], ["north-a"])
        self.assertEqual([m["source_id"] for m in entered_members], ["north-a"])

        # 覆盖质量单独说明：三个关闭窗口全部漂移，且明确这不是倾向变化。
        coverage = result["coverage"]
        self.assertFalse(coverage["passed"])
        self.assertEqual({problem["reason"] for problem in coverage["problems"]}, {"drifted"})
        self.assertEqual(len(coverage["frames"]), 3)
        self.assertIn("not a stance", coverage["interpretation"])
        frame_ids = {frame["frame_id"]: frame for frame in coverage["frames"]}
        self.assertTrue(frame_ids["f-north-a"]["drifted"])
        # north-a：撤回 c 与新增 e 数量抵消（当前仍 2 条），但成员集合已变，哈希漂移。
        self.assertEqual(frame_ids["f-north-a"]["frozen_actual_count"], 2)
        self.assertEqual(frame_ids["f-north-a"]["current_count"], 2)
        # north-b：后到转载使当前数 1→2。
        self.assertTrue(frame_ids["f-north-b"]["drifted"])
        self.assertEqual(frame_ids["f-north-b"]["current_count"], 2)
        # south-a：d 的修订替换头版本，计数同为 1 但内容哈希漂移。
        self.assertTrue(frame_ids["f-south"]["drifted"])
        self.assertEqual(frame_ids["f-south"]["current_count"], 1)

        # 观察级索引：撤回、更正、改判、新增各有一条可追溯记录。
        changes = {(row["origin"], row["external_id"]): row for row in result["observation_changes"]}
        self.assertIn("retracted", changes[("local-fixture", "c")]["reasons"])
        self.assertIn("corrected", changes[("local-fixture", "d")]["reasons"])
        self.assertIn("coding_changed", changes[("local-fixture", "d")]["reasons"])
        self.assertEqual(changes[("local-fixture", "b2")]["reasons"], ["entered_after_release"])
        # 撤回稿的完整修订链仍在历史证据中。
        c_lineage = changes[("local-fixture", "c")]["lineage"]
        self.assertEqual([row["revision"] for row in c_lineage], [1])
        self.assertEqual(c_lineage[0]["status"], "retracted")
        self.assertTrue(c_lineage[0]["reason"])

        # 事件时间线只统计冻结头之后的事件。
        timeline = result["event_timeline"]
        self.assertEqual(timeline["frozen_head"], frozen_after["event_head"])
        kinds = {event["kind"] for event in timeline["events"]}
        self.assertIn("observation_retracted", kinds)
        self.assertIn("observation_corrected", kinds)
        self.assertIn("case_adjudicated", kinds)
        self.assertIn("observation_ingested", kinds)

    def test_trace_observation_links_revisions_decisions_and_ruling(self):
        service = self.service
        self.build_frozen_release()
        self.post_release_changes()
        trace = service.attribution.trace_observation("november-2026", "local-fixture", "d")
        self.assertEqual(trace["frozen_snapshot"]["revision"], 1)
        self.assertEqual(trace["frozen_snapshot"]["stance"], "positive")
        self.assertEqual(trace["current_snapshot"]["revision"], 2)
        self.assertEqual(trace["current_snapshot"]["stance"], "negative")
        self.assertEqual([row["revision"] for row in trace["revisions"]], [1, 2])
        self.assertFalse(trace["revisions"][0]["is_head"])
        self.assertTrue(trace["revisions"][1]["is_head"])
        self.assertEqual(trace["revisions"][1]["coding_status"], "adjudicated")
        self.assertEqual(trace["current_adjudication"]["adjudicator"], "carol")
        self.assertEqual(trace["current_adjudication"]["stance"], "negative")
        event_kinds = [event["kind"] for event in trace["events"]]
        self.assertIn("case_adjudicated", event_kinds)

        # 撤回稿仍可追溯：冻结侧合格、当前侧 retracted，历史修订不删除。
        trace_c = service.attribution.trace_observation("november-2026", "local-fixture", "c")
        self.assertEqual(trace_c["frozen_snapshot"]["category"], "eligible")
        self.assertEqual(trace_c["current_snapshot"]["category"], "retracted")
        self.assertEqual(len(trace_c["revisions"]), 1)

    def test_export_bundle_is_verifiable_and_tamper_evident(self):
        service = self.service
        self.build_frozen_release()
        self.post_release_changes()
        bundle = service.attribution.export("november-2026")
        verified = service.attribution.verify(bundle)
        self.assertTrue(verified["valid"])
        with self.assertRaises(MeridianError) as caught:
            tampered = {**bundle, "attribution": {**bundle["attribution"], "frozen_head": 1}}
            service.attribution.verify(tampered)
        self.assertEqual(caught.exception.code, "bundle_hash_mismatch")
        with self.assertRaises(MeridianError) as caught:
            broken = {**bundle, "attribution": {**bundle["attribution"],
                                                "reconciliation": {"passed": False, "checks": []}}}
            service.attribution.verify(broken)
        self.assertEqual(caught.exception.code, "bundle_hash_mismatch")

    def test_unchanged_release_attributes_nothing_and_reconciles(self):
        service = self.service
        self.build_frozen_release()
        result = service.attribution.attribute("november-2026")
        self.assertTrue(result["reconciliation"]["passed"])
        self.assertEqual(result["totals"]["deltas"]["story_count"], 0)
        self.assertEqual(result["totals"]["deltas"]["eligible_observations"], 0)
        self.assertEqual(result["observation_changes"], [])
        self.assertTrue(result["coverage"]["passed"])
        for item in result["story_attributions"]:
            self.assertEqual(item["classification"], "unchanged")
            self.assertEqual(item["net_effect"]["story_row_delta"], 0)
            self.assertTrue(all(t["change"] == "bucket_unchanged"
                                for t in item["bucket_transitions"]))

    def test_topic_recode_splits_report_story_rows_and_still_reconciles(self):
        service = self.service
        spec = {"start": "2026-11-01T00:00:00+00:00", "end": "2026-12-01T00:00:00+00:00",
                "regions": ["North"], "topics": [], "audiences": []}
        service.observations.ingest(observation("a", "north-a", "story-x"))
        service.observations.ingest(observation("b", "north-b", "story-x"))
        coded(service, "a")
        coded(service, "b")
        preview = service.releases.preview(spec)
        service.releases.publish("split-rel", spec, preview["token"])

        # 发布后更正 a 并重新编码到 culture 话题：同一 story_key 分裂成两个报告故事行。
        corrected = observation("a", "north-a", "story-x") | {"headline": "topic recode"}
        service.observations.ingest(corrected, expected_revision=1)
        coded(service, "a", stance="negative", topic="culture", revision=2)

        result = service.attribution.attribute("split-rel")
        self.assertTrue(result["reconciliation"]["passed"],
                        [c for c in result["reconciliation"]["checks"] if not c["passed"]])
        story = next(item for item in result["story_attributions"]
                     if item["story_key"] == "story-x")
        self.assertIn("buckets_split", story["reasons"])
        self.assertEqual(len(story["frozen"]["bucket_rows"]), 1)
        self.assertEqual(len(story["current"]["bucket_rows"]), 2)
        # 故事单元仍是一个，但报告故事行净增 1；这正是不能只按 story_key 数分母的原因。
        self.assertEqual(story["net_effect"]["story_row_delta"], 1)
        self.assertEqual(story["net_effect"]["observation_count_delta"], 0)
        transitions = {(t["topic"], t["change"]) for t in story["bucket_transitions"]}
        self.assertIn(("culture", "bucket_added"), transitions)
        self.assertIn(("technology", "bucket_revised"), transitions)
        # 地区报告分母实际从 1 变为 2，归因净值与之完全相等。
        live = service.analytics.report(spec)
        self.assertEqual(live["story_count"], 2)
        self.assertEqual(result["totals"]["deltas"]["story_count"], 1)
        north = next(row for row in result["regions"] if row["region"] == "North")
        self.assertEqual(north["story_count_delta"], 1)

    def test_correction_without_recoding_moves_member_to_excluded(self):
        service = self.service
        spec = {"start": "2026-11-01T00:00:00+00:00", "end": "2026-12-01T00:00:00+00:00",
                "regions": ["North"], "topics": [], "audiences": []}
        service.observations.ingest(observation("a", "north-a", "story-7"))
        coded(service, "a")
        preview = service.releases.preview(spec)
        service.releases.publish("pending-rel", spec, preview["token"])

        # 更正后未重新编码：头修订进入 awaiting_review，故事从合格集合退出但观察仍在窗口。
        corrected = observation("a", "north-a", "story-7") | {"headline": "corrected, needs recode"}
        service.observations.ingest(corrected, expected_revision=1)
        result = service.attribution.attribute("pending-rel")
        self.assertTrue(result["reconciliation"]["passed"],
                        [c for c in result["reconciliation"]["checks"] if not c["passed"]])
        story = next(item for item in result["story_attributions"]
                     if item["story_key"] == "story-7")
        self.assertEqual(story["classification"], "exited")
        self.assertIn("member_became_excluded", story["reasons"])
        self.assertEqual(story["current"]["excluded"][0]["category"], "uncoded")
        self.assertEqual(story["net_effect"]["story_row_delta"], -1)
        # 窗口分母不变（观察没消失），只是从合格移入排除，恒等式对账必须成立。
        self.assertEqual(result["totals"]["deltas"]["observations_in_window"], 0)
        self.assertEqual(result["totals"]["deltas"]["eligible_observations"], -1)
        self.assertEqual(result["totals"]["deltas"]["excluded"]["uncoded"], 1)

    def test_frozen_membership_is_immune_to_late_repost(self):
        service = self.service
        self.build_frozen_release()
        service.observations.ingest(
            observation("late", "north-b", "story-1", "2026-11-06T09:00:00+00:00"))
        coded(service, "late")
        result = service.attribution.attribute("november-2026")
        frozen_checks = {check["check"]: check for check in result["reconciliation"]["checks"]}
        self.assertTrue(frozen_checks["frozen_membership_rebuilt_from_release"]["passed"])
        self.assertTrue(frozen_checks["frozen_membership_categories_unchanged"]["passed"])
        # 冻结报告里的 provenance 仍然只有发布时的 4 条观察。
        frozen_report = service.releases.describe("november-2026")["report"]
        self.assertEqual(len(frozen_report["provenance"]), 4)


class AttributionApiTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        from pathlib import Path
        from fastapi.testclient import TestClient
        from meridian.api import create_app
        self.temp = tempfile.TemporaryDirectory()
        path = str(Path(self.temp.name) / "api.sqlite3")
        self.app = create_app(path)
        self.client = TestClient(self.app)
        self.service = self.app.state.service

    def tearDown(self):
        self.app.state.service.close()
        self.temp.cleanup()

    def test_attribution_endpoints_end_to_end(self):
        service = self.service
        for source_id, region, outlet in (("north-a", "North", "Outlet A"),
                                          ("north-b", "North", "Outlet B")):
            service.sources.register(source(source_id, region, outlet))
        service.observations.ingest(observation("a", "north-a", "story-1"))
        service.observations.ingest(observation("b", "north-b", "story-1"))
        coded(service, "a")
        coded(service, "b")
        spec = {"start": "2026-11-01T00:00:00+00:00", "end": "2026-12-01T00:00:00+00:00",
                "regions": ["North"], "topics": [], "audiences": []}
        preview = service.releases.preview(spec)
        service.releases.publish("rel", spec, preview["token"])
        service.observations.retract("local-fixture", "b", "outlet withdrew item",
                                     expected_revision=1)

        response = self.client.get("/releases/rel/attribution")
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["reconciliation"]["passed"])
        story = next(item for item in payload["story_attributions"]
                     if item["story_key"] == "story-1")
        self.assertIn("member_retracted", story["reasons"])

        trace = self.client.get("/releases/rel/observations/local-fixture/b/trace")
        self.assertEqual(trace.status_code, 200)
        self.assertEqual(trace.json()["frozen_snapshot"]["category"], "eligible")
        self.assertEqual(trace.json()["current_snapshot"]["category"], "retracted")

        export_response = self.client.get("/releases/rel/attribution/export")
        self.assertEqual(export_response.status_code, 200)
        bundle = export_response.json()
        self.assertTrue(service.attribution.verify(bundle)["valid"])

        missing = self.client.get("/releases/unknown/attribution")
        self.assertEqual(missing.status_code, 404)
        self.assertEqual(missing.json()["error"], "release_missing")


if __name__ == "__main__":
    unittest.main()
