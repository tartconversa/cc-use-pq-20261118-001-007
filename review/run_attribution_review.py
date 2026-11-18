"""端到端复核脚本：构建一次冻结发布，施加发布后的撤回/更正/裁定/转载/新事件，
输出可人工阅读的差异归因报告与机器可核验的 JSON 证据包。

用法：
    python3 -I review/run_attribution_review.py [输出目录]

脚本只依赖仓库内的 meridian 包，使用临时 SQLite，不访问网络。
"""
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from meridian.demo import observation, source
from meridian.service import MeridianService


SPEC = {"start": "2026-11-01T00:00:00+00:00", "end": "2026-12-01T00:00:00+00:00",
        "regions": ["North", "South"], "topics": [], "audiences": []}


def coded(service, external_id, *, stance="positive", reviewers=("alice", "bob"), revision=1):
    for reviewer in reviewers:
        service.coding.submit(
            {"decision_id": f"{external_id}-{reviewer}-r{revision}",
             "origin": "local-fixture", "external_id": external_id, "revision": revision,
             "reviewer": reviewer, "topic": "technology", "stance": stance,
             "audience": "general",
             "reason": f"reviewer {reviewer} judgment on revision {revision}"})


def build_scenario():
    temp = tempfile.TemporaryDirectory()
    service = MeridianService(str(Path(temp.name) / "review.sqlite3"))
    service.sources.register(source("north-a", "North", "Outlet A"))
    service.sources.register(source("north-b", "North", "Outlet B"))
    service.sources.register(source("south-a", "South", "Outlet C"))

    # 发布前：三个采集窗口，四条观察、四个故事（story-1 由两家媒体共同覆盖）。
    service.frames.open("f-north-a", "north-a", SPEC["start"], SPEC["end"], 2)
    service.frames.open("f-north-b", "north-b", SPEC["start"], SPEC["end"], 1)
    service.frames.open("f-south", "south-a", SPEC["start"], SPEC["end"], 1)
    service.observations.ingest(observation("a", "north-a", "story-1"))
    service.observations.ingest(observation("c", "north-a", "story-3"))
    service.observations.ingest(observation("b", "north-b", "story-1"))
    service.observations.ingest(observation("d", "south-a", "story-4"))
    for external_id in ("a", "b", "c", "d"):
        coded(service, external_id)
    for frame_id in ("f-north-a", "f-north-b", "f-south"):
        service.frames.close(frame_id)
    preview = service.releases.preview(SPEC)
    assert preview["quality"]["passed"], preview["quality"]
    service.releases.publish("november-2026", SPEC, preview["token"])

    # 发布后事件：撤回、更正+裁定改判、后到转载、全新事件。
    service.observations.retract("local-fixture", "c",
                                 "source issued correction and withdrew", expected_revision=1)
    corrected = observation("d", "south-a", "story-4") | {"headline": "Corrected claims"}
    service.observations.ingest(corrected, expected_revision=1)
    coded(service, "d", stance="positive", revision=2, reviewers=("alice",))
    coded(service, "d", stance="neutral", revision=2, reviewers=("bob",))
    service.coding.adjudicate(
        {"origin": "local-fixture", "external_id": "d", "revision": 2,
         "adjudicator": "carol", "topic": "technology", "stance": "negative",
         "audience": "general",
         "reason": "ruling on corrected revision: evidence supports negative"})
    service.observations.ingest(
        observation("b2", "north-b", "story-1", "2026-11-05T09:00:00+00:00"))
    coded(service, "b2")
    service.observations.ingest(
        observation("e", "north-a", "story-5", "2026-11-08T09:00:00+00:00"))
    coded(service, "e")
    return service, temp


def render_text(attribution):
    lines = []
    totals = attribution["totals"]
    lines.append("Meridian 冻结发布差异归因（release: %s）" % attribution["release_id"])
    lines.append("事件头: 冻结 %s → 当前 %s" % (attribution["frozen_head"],
                                              attribution["current_head"]))
    lines.append("")
    lines.append("一、完整分母（两版各自保留，不互相覆盖）")
    for side in ("frozen", "current"):
        data = totals[side]
        lines.append("  %-7s 故事=%d  窗口观察=%d  合格观察=%d  排除=%s"
                     % (side, data["story_count"], data["observations_in_window"],
                        data["eligible_observations"], json.dumps(data["excluded"], ensure_ascii=False)))
    lines.append("  差值       故事=%+d 窗口观察=%+d 合格观察=%+d"
                 % (totals["deltas"]["story_count"], totals["deltas"]["observations_in_window"],
                    totals["deltas"]["eligible_observations"]))
    lines.append("")
    lines.append("二、地区结果（份额差注明含分母效应，不能直接当作倾向漂移）")
    for region in attribution["regions"]:
        lines.append("  [%s] 故事分母 %d→%d (%+d)，合格观察 %d→%d (%+d)"
                     % (region["region"], region["frozen_story_denominator"],
                        region["current_story_denominator"], region["story_count_delta"],
                        region["frozen_observation_count"], region["current_observation_count"],
                        region["observation_count_delta"]))
        for stance, row in sorted(region["stances"].items()):
            lines.append("      %-8s 计数 %d→%d (%+d)  份额 %s→%s"
                         % (stance, row["frozen_count"], row["current_count"], row["count_delta"],
                            row["frozen_share"], row["current_share"]))
        for row in region["source_eligible_delta"]:
            lines.append("      来源 %s 合格观察 %d→%d (%+d)"
                         % (row["source_id"], row["frozen_eligible_observations"],
                            row["current_eligible_observations"], row["eligible_delta"]))
    lines.append("")
    lines.append("三、逐故事归因（同故事多变化只给一个净效应，不拆项相加）")
    for item in attribution["story_attributions"]:
        net = item["net_effect"]
        lines.append("  [%s/%s] %s  原因=%s"
                     % (item["region"], item["story_key"], item["classification"],
                        ", ".join(item["reasons"]) or "无"))
        lines.append("      冻结成员=%s 当前成员=%s"
                     % ([f'{m["origin"]}/{m["external_id"]}' for m in item["frozen"]["members"]],
                        [f'{m["origin"]}/{m["external_id"]}' for m in item["current"]["members"]]))
        lines.append("      净效应: 报告故事行%+d 观察%+d 成员倾向计数差=%s"
                     % (net["story_row_delta"], net["observation_count_delta"],
                        json.dumps(net["stance_count_delta"], ensure_ascii=False)))
        for transition in item["bucket_transitions"]:
            lines.append("      桶行 (%s/%s) %s 故事级倾向差=%s"
                         % (transition["topic"], transition["audience"], transition["change"],
                            json.dumps(transition["story_stance_delta"], ensure_ascii=False)))
        for change in item["member_changes"]:
            lines.append("      成员变化 %s/%s: %s (rev %s→%s, stance %s→%s)"
                         % (change["origin"], change["external_id"], change["reason"],
                            change["before"]["revision"] if change["before"] else None,
                            change["after"]["revision"] if change["after"] else None,
                            change["before"]["stance"] if change["before"] else None,
                            change["after"]["stance"] if change["after"] else None))
    lines.append("")
    lines.append("四、采集覆盖质量（与倾向分析分开报告）")
    coverage = attribution["coverage"]
    lines.append("  覆盖检查通过=%s；问题=%s"
                 % (coverage["passed"], json.dumps(coverage["problems"], ensure_ascii=False)))
    for frame in coverage["frames"]:
        lines.append("  窗口 %s (%s/%s): state=%s 封档数=%s 当前数=%d 漂移=%s"
                     % (frame["frame_id"], frame["region"], frame["outlet"], frame["state"],
                        frame["frozen_actual_count"], frame["current_count"], frame["drifted"]))
    lines.append("  解释: " + coverage["interpretation"])
    lines.append("")
    lines.append("五、对账结果")
    for check in attribution["reconciliation"]["checks"]:
        lines.append("  [%s] %s (expected=%s actual=%s)"
                     % ("PASS" if check["passed"] else "FAIL", check["check"],
                        check["expected"], check["actual"]))
    lines.append("")
    lines.append("六、发布后事件时间线（只影响当前视图，不改原报告）")
    for event in attribution["event_timeline"]["events"]:
        lines.append("  #%s %s %s" % (event["event_no"], event["kind"], event["entity_key"]))
    return "\n".join(lines)


def main():
    output_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parent / "output"
    output_dir.mkdir(parents=True, exist_ok=True)
    service, temp = build_scenario()
    try:
        attribution = service.attribution.attribute("november-2026")
        assert attribution["reconciliation"]["passed"]
        bundle = service.attribution.export("november-2026")
        verified = service.attribution.verify(bundle)
        trace_d = service.attribution.trace_observation("november-2026", "local-fixture", "d")
        trace_c = service.attribution.trace_observation("november-2026", "local-fixture", "c")

        (output_dir / "attribution.json").write_text(
            json.dumps(attribution, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
        (output_dir / "attribution_bundle.json").write_text(
            json.dumps(bundle, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
        (output_dir / "trace_d_corrected_adjudicated.json").write_text(
            json.dumps(trace_d, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
        (output_dir / "trace_c_retracted.json").write_text(
            json.dumps(trace_c, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
        report = render_text(attribution)
        (output_dir / "attribution_report.txt").write_text(report, encoding="utf-8")
        print(report)
        print("\n导出包核验:", json.dumps(verified, ensure_ascii=False))
        print("产物目录:", output_dir)
    finally:
        service.close()
        temp.cleanup()


if __name__ == "__main__":
    main()
