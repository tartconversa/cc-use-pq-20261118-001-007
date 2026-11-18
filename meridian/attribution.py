"""冻结发布与当前视图之间可复核的差异归因。

归因以"故事"（region + story_key）为单元：一个故事在两版之间无论叠加了多少种
变化（撤回、更正、转载合并、裁定改判），只输出一个净效应，绝不把逐项差值相加
冒充总变化。所有净值都可以通过 reconciliation 块与两版报告的实际差值对账。

历史不可变原则：
- 撤回的观察保留在观察修订台账中，仍然出现在冻结侧证据与血缘里；
- 后到转载只出现在当前侧，冻结成员关系始终从已发布报告的 provenance 重建；
- 采集窗口漂移（frame drift / open）在 coverage 块单独说明，不计入倾向变化。
"""
from collections import Counter, defaultdict
from .contracts import digest
from .errors import require


class ReleaseAttribution:
    def __init__(self, store, observations, sources, coding, analytics, frames, releases):
        self.store = store
        self.observations = observations
        self.sources = sources
        self.coding = coding
        self.analytics = analytics
        self.frames = frames
        self.releases = releases

    # ------------------------------------------------------------------ 快照重建

    def _observation_record(self, origin, external_id, revision, category, *, frozen=False):
        """把某个修订版本与当时的编码结论组装成一条带血缘锚点的记录。

        frozen=True 时，观察状态以已发布 provenance 的 category 为准，并把观察状态钉为
        active（能进入发布证据的记录在发布时必然是 active），避免撤回等发布后的变化
        回写污染冻结侧；当前侧才读取实时 head 状态。
        """
        item = self.observations.describe(origin, external_id, revision)
        data = item["data"]
        source = self.sources.describe(data["source_id"])
        state = self.coding.status(origin, external_id, revision)
        coding = state.get("coding")
        observation_status = "active" if frozen else item["status"]
        return {"origin": origin, "external_id": external_id, "revision": revision,
                "is_head": item["is_head"], "observation_status": observation_status,
                "retraction_reason": None if frozen else item["reason"],
                "story_key": data["story_key"], "source_id": data["source_id"],
                "region": source["region"], "outlet": source["outlet"],
                "published_at": data["published_at"], "received_at": data["received_at"],
                "payload_hash": item["hash"], "supersedes": item["supersedes"],
                "category": category, "review_status": state["status"],
                "coding": coding,
                "decision_ids": [row["decision_id"] for row in state["decisions"]]}

    def _frozen_snapshot(self, release):
        """从已发布报告的 provenance 重建冻结时的窗口成员，不被后来的数据改写。"""
        report = release["report"]
        records = {}
        for entry in report["provenance"]:
            key = (entry["origin"], entry["external_id"])
            records[key] = self._observation_record(entry["origin"], entry["external_id"],
                                                    entry["revision"], entry["category"],
                                                    frozen=True)
        return records

    def _current_snapshot(self, spec):
        """当前视图：对同一查询即时重算，头修订 + 当前编码状态。"""
        records = {}
        for row in self.analytics._rows(spec):
            item = row["item"]
            data = item["data"]
            key = (data["origin"], data["external_id"])
            records[key] = self._observation_record(data["origin"], data["external_id"],
                                                    item["revision"],
                                                    self.analytics._classify(row, spec))
        return records

    @staticmethod
    def _group_records(records):
        """按 (region, story_key) 聚合故事单元；故事单元内部再按与报告同口径的
        (topic, audience) 桶行（bucket）划分，编码改题会导致桶行分裂/合并。"""
        groups = defaultdict(lambda: {"buckets": defaultdict(lambda: {"stances": Counter(),
                                                                       "members": {}}),
                                      "members": {}, "excluded": {}})
        for key, record in sorted(records.items()):
            group_key = (record["region"], record["story_key"])
            group = groups[group_key]
            if record["category"] != "eligible":
                group["excluded"][key] = record
                continue
            coding = record["coding"]
            bucket_key = (coding["topic"], coding["audience"])
            group["members"][key] = record
            group["buckets"][bucket_key]["members"][key] = record
            group["buckets"][bucket_key]["stances"][coding["stance"]] += 1
        return groups

    @staticmethod
    def _bucket_rows(group):
        """把桶行整理成与 analytics.report 故事行同口径的可对账结构。"""
        rows = []
        for (topic, audience), bucket in sorted(group["buckets"].items()):
            stances = bucket["stances"]
            story_stance = next(iter(stances)) if len(set(stances)) == 1 else "mixed"
            rows.append({"topic": topic, "audience": audience,
                         "story_stance": story_stance,
                         "stance_counts": dict(sorted(stances.items())),
                         "observation_count": sum(stances.values()),
                         "member_keys": sorted(bucket["members"])})
        return rows

    # ------------------------------------------------------------- 单故事差异归因

    @staticmethod
    def _member_state(record):
        if record is None:
            return None
        coding = record["coding"]
        return {"origin": record["origin"], "external_id": record["external_id"],
                "revision": record["revision"], "story_key": record["story_key"],
                "category": record["category"], "observation_status": record["observation_status"],
                "review_status": record["review_status"],
                "stance": coding["stance"] if coding else None,
                "topic": coding["topic"] if coding else None,
                "audience": coding["audience"] if coding else None,
                "source_id": record["source_id"], "outlet": record["outlet"]}

    def _classify_group(self, frozen, current):
        frozen_members = frozen["members"]
        current_members = current["members"]
        frozen_keys = set(frozen_members)
        current_keys = set(current_members)
        reasons = []
        member_changes = []
        for member_key in sorted(frozen_keys | current_keys):
            before = self._member_state(frozen_members.get(member_key))
            after = self._member_state(current_members.get(member_key))
            if before is not None and after is None:
                # 成员退出合格集合：先在同组排除映射里确认具体原因（撤回/未编码/争议等）。
                excluded_record = current.get("excluded", {}).get(member_key)
                if excluded_record is not None and excluded_record["category"] == "retracted":
                    reason = "member_retracted"
                elif excluded_record is not None:
                    reason = "member_became_excluded"
                else:
                    reason = "member_left_window"
                reasons.append(reason)
                member_changes.append({"origin": member_key[0], "external_id": member_key[1],
                                       "reason": reason, "before": before, "after": after})
                continue
            if before is None and after is not None:
                reason = "member_merged" if frozen_members else "member_entered"
                reasons.append(reason)
            elif before != after:
                # 同一成员可能同时承载多种变化（如更正后重新编码并被裁定改判），
                # 逐项打标签但只产出一条 member_change，净值仍然只算一次。
                if before["revision"] != after["revision"]:
                    reasons.append("member_corrected")
                if before["stance"] != after["stance"] or before["topic"] != after["topic"] \
                        or before["audience"] != after["audience"]:
                    reasons.append("member_recoded")
                if before["observation_status"] != after["observation_status"] \
                        or before["category"] != after["category"]:
                    reasons.append("member_state_changed")
                reason = "member_corrected" if "member_corrected" in reasons else \
                         "member_recoded" if "member_recoded" in reasons else "member_state_changed"
            else:
                continue
            member_changes.append({"origin": member_key[0], "external_id": member_key[1],
                                   "reason": reason, "before": before, "after": after})
        # 合格成员变为排除项（撤回/未编码/争议/题域外等）也要进入故事级原因；
        # 上面成员循环已为退出成员定过性，这里只补充没有 member_change 覆盖的情况。
        for member_key, record in sorted(current.get("excluded", {}).items()):
            if member_key not in frozen_members:
                continue
            label = "member_retracted" if record["category"] == "retracted" else "member_became_excluded"
            if label not in reasons:
                reasons.append(label)
        # 桶行（报告故事行）数量变化：编码改题/受众导致同一 story_key 分裂或合并。
        frozen_bucket_count = len(frozen["buckets"])
        current_bucket_count = len(current["buckets"])
        if frozen_bucket_count and current_bucket_count and frozen_bucket_count != current_bucket_count:
            reasons.append("buckets_merged" if frozen_bucket_count > current_bucket_count
                           else "buckets_split")
        if not frozen_members:
            classification = "entered"
        elif not current_members:
            classification = "exited"
        else:
            classification = "unchanged" if not member_changes else "revised"
        return classification, sorted(set(reasons)), member_changes

    @staticmethod
    def _row_signature(row):
        return (row["topic"], row["audience"])

    def _bucket_transitions(self, frozen, current):
        """桶行级匹配，与 analytics.report 的故事行同口径，是对账的最小净值单元。

        匹配键 (topic, audience)：两侧都有则 revised（可能倾向改变）；
        仅冻结侧有为 removed（故事行退出），仅当前侧有为 added（故事行进入）。
        """
        before_rows = {self._row_signature(row): row for row in self._bucket_rows(frozen)}
        after_rows = {self._row_signature(row): row for row in self._bucket_rows(current)}
        transitions = []
        for signature in sorted(set(before_rows) | set(after_rows)):
            left = before_rows.get(signature)
            right = after_rows.get(signature)
            if left is None:
                change = "bucket_added"
            elif right is None:
                change = "bucket_removed"
            elif left["story_stance"] != right["story_stance"] or \
                    left["stance_counts"] != right["stance_counts"]:
                change = "bucket_revised"
            else:
                change = "bucket_unchanged"
            transitions.append({"topic": signature[0], "audience": signature[1], "change": change,
                                "frozen": left, "current": right,
                                "story_stance_delta": self._row_stance_delta(left, right)})
        return transitions

    @staticmethod
    def _row_stance_delta(left, right):
        delta = Counter()
        for row, sign in ((left, -1), (right, 1)):
            if row is not None:
                delta[row["story_stance"]] += sign
        return dict(sorted(delta.items()))

    def _net_effect(self, frozen, current):
        """故事单元净效应：直接用两版聚合值相减，每个口径只出现一次，不按原因拆项相加。

        - story_row_delta：报告故事行（topic/audience 桶行）数量差，与 report.story_count 同口径；
        - observation_count_delta：合格成员观察数差；
        - stance_count_delta：成员观察层面的倾向计数差；
        - bucket_transitions 内的 story_stance_delta 才与报告地区 stance_counts 同口径。
        """
        frozen_rows = self._bucket_rows(frozen)
        current_rows = self._bucket_rows(current)
        frozen_stances = Counter()
        current_stances = Counter()
        for row in frozen_rows:
            frozen_stances.update(row["stance_counts"])
        for row in current_rows:
            current_stances.update(row["stance_counts"])
        stances = sorted(set(frozen_stances) | set(current_stances))
        story_stance = Counter()
        for rows, sign in ((frozen_rows, -1), (current_rows, 1)):
            for row in rows:
                story_stance[row["story_stance"]] += sign
        return {
            "story_row_delta": len(current_rows) - len(frozen_rows),
            "observation_count_delta": sum(current_stances.values()) - sum(frozen_stances.values()),
            "stance_count_delta": {stance: current_stances.get(stance, 0)
                                   - frozen_stances.get(stance, 0) for stance in stances},
            "story_stance_delta": dict(sorted(story_stance.items())),
            "frozen_stance_counts": dict(sorted(frozen_stances.items())),
            "current_stance_counts": dict(sorted(current_stances.items())),
        }

    def _group_attribution(self, key, frozen_groups, current_groups):
        region, story_key = key
        frozen = frozen_groups.get(key, {"buckets": {}, "members": {}, "excluded": {}})
        current = current_groups.get(key, {"buckets": {}, "members": {}, "excluded": {}})
        classification, reasons, member_changes = self._classify_group(frozen, current)
        net = self._net_effect(frozen, current)
        return {
            "region": region, "story_key": story_key, "classification": classification,
            "reasons": reasons,
            "frozen": {"present": bool(frozen["members"]),
                       "bucket_rows": self._bucket_rows(frozen),
                       "observation_count": len(frozen["members"]),
                       "members": [self._member_state(record)
                                   for record in sorted(frozen["members"].values(),
                                                        key=lambda item: (item["origin"], item["external_id"]))],
                       "excluded": [{"origin": member[0], "external_id": member[1],
                                     "category": record["category"]}
                                    for member, record in sorted(frozen["excluded"].items())]},
            "current": {"present": bool(current["members"]),
                        "bucket_rows": self._bucket_rows(current),
                        "observation_count": len(current["members"]),
                        "members": [self._member_state(record)
                                    for record in sorted(current["members"].values(),
                                                         key=lambda item: (item["origin"], item["external_id"]))],
                        "excluded": [{"origin": member[0], "external_id": member[1],
                                      "category": record["category"]}
                                     for member, record in sorted(current["excluded"].items())]},
            "bucket_transitions": self._bucket_transitions(frozen, current),
            "member_changes": member_changes,
            "net_effect": net,
        }

    # ------------------------------------------------------------- 地区与来源汇总

    @staticmethod
    def _counter_index(rows, key):
        return {row[key]: row for row in rows}

    def _region_summaries(self, frozen_report, current_report, group_attributions):
        before_index = self._counter_index(frozen_report["regions"], "region")
        after_index = self._counter_index(current_report["regions"], "region")
        summaries = []
        for region in sorted(set(before_index) | set(after_index)):
            before = before_index.get(region)
            after = after_index.get(region)
            before_count = before["story_count"] if before else 0
            after_count = after["story_count"] if after else 0
            before_stances = before["stance_counts"] if before else {}
            after_stances = after["stance_counts"] if after else {}
            stances = {}
            for stance in sorted(set(before_stances) | set(after_stances)):
                old = before_stances.get(stance, 0)
                new = after_stances.get(stance, 0)
                old_share = round(old / before_count, 6) if before_count else None
                new_share = round(new / after_count, 6) if after_count else None
                stances[stance] = {
                    "frozen_count": old, "current_count": new, "count_delta": new - old,
                    "frozen_share": old_share, "current_share": new_share,
                    "share_delta": (round(new_share - old_share, 6)
                                    if old_share is not None and new_share is not None else None),
                    "share_delta_note": "share changes blend denominator (membership) effects and "
                                        "coding effects; use count_delta plus per-story net_effect "
                                        "to separate them",
                }
            source_before = Counter()
            source_after = Counter()
            outlet_before = Counter()
            outlet_after = Counter()
            for attribution in group_attributions:
                if attribution["region"] != region:
                    continue
                for member in attribution["frozen"]["members"]:
                    source_before[member["source_id"]] += 1
                    outlet_before[member["outlet"]] += 1
                for member in attribution["current"]["members"]:
                    source_after[member["source_id"]] += 1
                    outlet_after[member["outlet"]] += 1
            sources = []
            for source_id in sorted(set(source_before) | set(source_after)):
                old = source_before.get(source_id, 0)
                new = source_after.get(source_id, 0)
                sources.append({"source_id": source_id,
                                "frozen_eligible_observations": old,
                                "current_eligible_observations": new,
                                "eligible_delta": new - old})
            outlets = []
            for outlet in sorted(set(outlet_before) | set(outlet_after)):
                old = outlet_before.get(outlet, 0)
                new = outlet_after.get(outlet, 0)
                outlets.append({"outlet": outlet, "frozen_eligible_observations": old,
                                "current_eligible_observations": new, "eligible_delta": new - old})
            summaries.append({
                "region": region,
                "frozen_story_denominator": before_count, "current_story_denominator": after_count,
                "story_count_delta": after_count - before_count,
                "frozen_observation_count": before["observation_count"] if before else 0,
                "current_observation_count": after["observation_count"] if after else 0,
                "observation_count_delta": (after["observation_count"] if after else 0)
                                            - (before["observation_count"] if before else 0),
                "stances": stances, "source_eligible_delta": sources,
                "outlet_eligible_delta": outlets,
            })
        return summaries

    # ------------------------------------------------------------- 覆盖质量（单独说明）

    def _coverage_section(self, spec, frozen_head):
        coverage = self.frames.quality(spec)
        frames = []
        for frame in coverage["frames"]:
            source = self.sources.describe(frame["source_id"])
            problems = []
            if frame["state"] != "closed":
                problems.append("open")
            if frame["drifted"]:
                problems.append("drifted")
            frames.append({"frame_id": frame["frame_id"], "source_id": frame["source_id"],
                           "region": source["region"], "outlet": source["outlet"],
                           "start": frame["start"], "end": frame["end"],
                           "state": frame["state"], "expected_count": frame["expected_count"],
                           "frozen_actual_count": frame["actual_count"],
                           "current_count": frame["current_count"], "drifted": frame["drifted"],
                           "problems": problems,
                           "note": ("frame_members stay frozen at release; drift means the live "
                                    "window no longer matches the sealed coverage contract")})
        return {"passed": coverage["passed"],
                "problems": [dict(problem) for problem in coverage["problems"]],
                "frames": frames,
                "interpretation": "coverage drift is a collection-quality finding, not a stance "
                                  "change; never report missing coverage as a shifted lean",
                "frozen_head": frozen_head, "current_head": self.store.head()}

    # ------------------------------------------------------------- 观察级血缘索引

    def _observation_section(self, frozen_records, current_records):
        observations = []
        for key in sorted(set(frozen_records) | set(current_records)):
            before = frozen_records.get(key)
            after = current_records.get(key)
            if before is not None and after is not None and \
                    self._member_state(before) == self._member_state(after):
                continue
            if before is None:
                reasons = ["entered_after_release"]
            elif after is None:
                reasons = ["left_current_window"]
            else:
                reasons = []
                if after["observation_status"] == "retracted" and \
                        before["observation_status"] != "retracted":
                    reasons.append("retracted")
                if after["revision"] != before["revision"]:
                    reasons.append("corrected")
                before_coding = before["coding"]
                after_coding = after["coding"]
                if before_coding != after_coding:
                    reasons.append("coding_changed")
                if after["category"] != before["category"]:
                    reasons.append("classification_changed")
            lineage = self.observations.lineage(*key)
            observations.append({
                "origin": key[0], "external_id": key[1],
                "reasons": reasons,
                "frozen": self._member_state(before), "current": self._member_state(after),
                "lineage": [{"revision": item["revision"], "is_head": item["is_head"],
                             "status": item["status"], "reason": item["reason"],
                             "payload_hash": item["hash"], "supersedes": item["supersedes"],
                             "story_key": item["data"]["story_key"],
                             "source_id": item["data"]["source_id"]} for item in lineage],
                "revisions_detail": [
                    {"revision": item["revision"],
                     "coding_status": self.coding.status(key[0], key[1], item["revision"])["status"],
                     "decision_ids": [row["decision_id"] for row in
                                      self.coding.decisions_for(key[0], key[1], item["revision"])]}
                    for item in lineage],
            })
        return observations

    def _event_timeline(self, frozen_head):
        # 严格取发布事件头之后的事件；发布事件本身不属于"发布后的变化"。
        rows = self.store.all(
            "SELECT * FROM events WHERE event_no>? ORDER BY event_no", (frozen_head,))
        events = [{"event_no": row["event_no"], "kind": row["kind"], "entity_key": row["entity_key"],
                   "payload_hash": row["payload_hash"], "occurred_at": row["occurred_at"]}
                  for row in rows if row["kind"] != "report_published"]
        by_observation = defaultdict(list)
        for event in events:
            parts = event["entity_key"].split("/")
            if len(parts) >= 2 and event["kind"] in {
                    "observation_ingested", "observation_corrected", "observation_retracted",
                    "coding_submitted", "case_adjudicated"}:
                # 观察事件键为 origin/external_id；裁定事件键为 origin/external_id/revision。
                by_observation[f"{parts[0]}/{parts[1]}"].append(event["event_no"])
        return {"frozen_head": frozen_head, "current_head": self.store.head(),
                "events": events, "events_by_observation": dict(sorted(by_observation.items()))}

    # ------------------------------------------------------------- 主入口与对账

    def attribute(self, release_id):
        release = self.releases.describe(release_id)
        frozen_report = release["report"]
        spec = frozen_report["query"]
        frozen_head = release["event_head"]
        current_report = self.analytics.report(spec)

        frozen_records = self._frozen_snapshot(release)
        current_records = self._current_snapshot(spec)
        frozen_groups = self._group_records(frozen_records)
        current_groups = self._group_records(current_records)

        all_keys = sorted(set(frozen_groups) | set(current_groups))
        group_attributions = [
            self._group_attribution(key, frozen_groups, current_groups) for key in all_keys]

        region_summaries = self._region_summaries(frozen_report, current_report, group_attributions)
        reconciliation = self._reconcile(
            frozen_report, current_report, group_attributions,
            frozen_records, current_records, region_summaries)
        return {
            "release_id": release_id,
            "query": spec, "query_hash": frozen_report["query_hash"],
            "frozen_head": frozen_head, "current_head": current_report["event_head"],
            "totals": self._totals(frozen_report, current_report),
            "regions": region_summaries,
            "story_attributions": group_attributions,
            "observation_changes": self._observation_section(frozen_records, current_records),
            "coverage": self._coverage_section(spec, frozen_head),
            "event_timeline": self._event_timeline(frozen_head),
            "reconciliation": reconciliation,
        }

    @staticmethod
    def _totals(frozen_report, current_report):
        excluded_kinds = sorted(set(frozen_report["excluded"]) | set(current_report["excluded"]))
        return {
            "frozen": {"story_count": frozen_report["story_count"],
                       "observations_in_window": frozen_report["observations_in_window"],
                       "eligible_observations": frozen_report["eligible_observations"],
                       "excluded": dict(sorted(frozen_report["excluded"].items()))},
            "current": {"story_count": current_report["story_count"],
                        "observations_in_window": current_report["observations_in_window"],
                        "eligible_observations": current_report["eligible_observations"],
                        "excluded": dict(sorted(current_report["excluded"].items()))},
            "deltas": {"story_count": current_report["story_count"] - frozen_report["story_count"],
                       "observations_in_window": (current_report["observations_in_window"]
                                                  - frozen_report["observations_in_window"]),
                       "eligible_observations": (current_report["eligible_observations"]
                                                 - frozen_report["eligible_observations"]),
                       # 差值视图保留并集键（含 0），方便逐类对账；分母视图不做零填充。
                       "excluded": {kind: current_report["excluded"].get(kind, 0)
                                    - frozen_report["excluded"].get(kind, 0)
                                    for kind in excluded_kinds}},
        }

    def _reconcile(self, frozen_report, current_report, group_attributions,
                   frozen_records, current_records, region_summaries):
        checks = []

        def check(name, expected, actual, detail=None):
            checks.append({"check": name, "passed": expected == actual,
                           "expected": expected, "actual": actual, "detail": detail})

        # 1) 报告故事行：各故事单元桶行净值之和 == 两版报告 story_count 实际差值
        #    （编码改题导致的桶行分裂/合并在这里自然计入，不与成员级变化重复相加）。
        story_delta_sum = sum(item["net_effect"]["story_row_delta"] for item in group_attributions)
        check("story_delta_sum_equals_report",
              current_report["story_count"] - frozen_report["story_count"], story_delta_sum)

        # 2) 合格观察数：故事单元观察净值之和 == 报告差值
        observation_delta_sum = sum(item["net_effect"]["observation_count_delta"]
                                    for item in group_attributions)
        check("eligible_observation_delta_sum_equals_report",
              current_report["eligible_observations"] - frozen_report["eligible_observations"],
              observation_delta_sum)

        # 3) 各地区各倾向计数：桶行（报告故事行）级倾向净值汇总 == 报告地区行 stance_counts 差
        #    （成员观察口径与故事行口径不同，不可混用，分歧故事在报告里记为 mixed）。
        region_stance = defaultdict(Counter)
        for item in group_attributions:
            for transition in item["bucket_transitions"]:
                for stance, delta in transition["story_stance_delta"].items():
                    region_stance[item["region"]][stance] += delta
        for summary in region_summaries:
            for stance, row in summary["stances"].items():
                check(f"region_stance_delta:{summary['region']}:{stance}",
                      row["count_delta"], region_stance[summary["region"]].get(stance, 0))

        # 4) 地区报告故事行净值 == 报告地区分母差
        for summary in region_summaries:
            region_groups = [item for item in group_attributions
                             if item["region"] == summary["region"]]
            check(f"region_story_denominator:{summary['region']}",
                  summary["story_count_delta"],
                  sum(item["net_effect"]["story_row_delta"] for item in region_groups))

        # 4b) 来源级合格观察净值之和 == 报告合格观察总数差（防止来源维度与故事维度对不上）
        source_net = sum(row["eligible_delta"] for summary in region_summaries
                         for row in summary["source_eligible_delta"])
        check("source_eligible_delta_sum_equals_report",
              current_report["eligible_observations"] - frozen_report["eligible_observations"],
              source_net)

        # 5) 窗口观察恒等式：窗口差 = 合格差 + 排除差
        exclusion_delta = sum(current_report["excluded"].get(kind, 0)
                              - frozen_report["excluded"].get(kind, 0)
                              for kind in set(current_report["excluded"]) | set(frozen_report["excluded"]))
        check("window_identity_eligible_plus_excluded",
              current_report["observations_in_window"] - frozen_report["observations_in_window"],
              (current_report["eligible_observations"] - frozen_report["eligible_observations"])
              + exclusion_delta)

        # 6) 冻结侧重建成员与已发布报告 provenance 完全一致（成员关系不可被后到数据改写）
        frozen_provenance = {(row["origin"], row["external_id"]): row
                             for row in frozen_report["provenance"]}
        check("frozen_membership_rebuilt_from_release",
              len(frozen_provenance), len(frozen_records),
              {"categories": dict(Counter(row["category"] for row in frozen_provenance.values()))})
        frozen_match = all(
            frozen_records[key]["category"] == row["category"]
            and frozen_records[key]["revision"] == row["revision"]
            for key, row in frozen_provenance.items())
        checks.append({"check": "frozen_membership_categories_unchanged", "passed": frozen_match,
                       "expected": True, "actual": frozen_match,
                       "detail": "late reposts and retractions never rewrite the released membership"})

        # 7) 当前侧重建与即时报告 provenance 一致
        current_provenance = {(row["origin"], row["external_id"]): row
                              for row in current_report["provenance"]}
        current_match = (set(current_provenance) == set(current_records)
                         and all(current_records[key]["category"] == row["category"]
                                 and current_records[key]["revision"] == row["revision"]
                                 for key, row in current_provenance.items()))
        checks.append({"check": "current_membership_matches_live_report", "passed": current_match,
                       "expected": True, "actual": current_match, "detail": None})

        # 8) 撤回稿仍在历史证据中（不物理删除）
        retracted_keys = [key for key, record in current_records.items()
                          if record["category"] == "retracted"]
        retained = all(self.observations.lineage(*key) for key in retracted_keys)
        checks.append({"check": "retracted_observations_retained_in_lineage", "passed": retained,
                       "expected": True, "actual": retained,
                       "detail": {"retracted": [{"origin": key[0], "external_id": key[1]}
                                                for key in retracted_keys]}})

        return {"passed": all(check["passed"] for check in checks), "checks": checks}

    def trace_observation(self, release_id, origin, external_id):
        """单条观察的完整血缘：修订、每版审核结论、事件流，以及它在两版中的位置。"""
        release = self.releases.describe(release_id)
        spec = release["report"]["query"]
        frozen_records = self._frozen_snapshot(release)
        current_records = self._current_snapshot(spec)
        key = (origin, external_id)
        lineage = self.observations.lineage(origin, external_id)
        revisions = []
        for item in lineage:
            state = self.coding.status(origin, external_id, item["revision"])
            revisions.append({
                "revision": item["revision"], "is_head": item["is_head"],
                "observation_status": item["status"], "retraction_reason": item["reason"],
                "payload_hash": item["hash"], "supersedes": item["supersedes"],
                "data": item["data"],
                "coding_status": state["status"], "coding": state.get("coding"),
                "decision_ids": [row["decision_id"] for row in state["decisions"]],
            })
        adjudication_key = f"{origin}/{external_id}/{lineage[-1]['revision']}"
        ruling = self.store.one("SELECT * FROM adjudications WHERE case_key=?", (adjudication_key,))
        events = self.store.all(
            "SELECT event_no,kind,entity_key,payload_hash,occurred_at FROM events "
            "WHERE entity_key=? OR entity_key LIKE ? ORDER BY event_no",
            (f"{origin}/{external_id}", f"{origin}/{external_id}/%"))
        frozen_record = frozen_records.get(key)
        current_record = current_records.get(key)
        return {
            "release_id": release_id, "origin": origin, "external_id": external_id,
            "frozen_snapshot": self._member_state(frozen_record),
            "current_snapshot": self._member_state(current_record),
            "revisions": revisions,
            "current_adjudication": ({field: ruling[field] for field in
                                      ("adjudicator", "topic", "stance", "audience", "reason", "event_no")}
                                     if ruling else None),
            "events": [dict(row) for row in events],
            "frozen_head": release["event_head"], "current_head": self.store.head(),
        }

    # ------------------------------------------------------------- 可独立核验的导出包

    def export(self, release_id):
        attribution = self.attribute(release_id)
        bundle = {"kind": "release_attribution", "attribution": attribution}
        bundle["bundle_hash"] = digest(bundle)
        return bundle

    @staticmethod
    def verify(bundle):
        require(isinstance(bundle, dict), "invalid_bundle", "bundle must be an object")
        require({"kind", "attribution", "bundle_hash"} <= set(bundle),
                "invalid_bundle", "bundle fields missing")
        actual = digest({key: value for key, value in bundle.items() if key != "bundle_hash"})
        require(actual == bundle["bundle_hash"], "bundle_hash_mismatch", "bundle content changed")
        attribution = bundle["attribution"]
        require(attribution["reconciliation"]["passed"], "attribution_reconciliation_failed",
                "attribution does not reconcile to the two report versions")
        return {"valid": True, "bundle_hash": bundle["bundle_hash"],
                "release_id": attribution["release_id"],
                "frozen_head": attribution["frozen_head"],
                "current_head": attribution["current_head"],
                "stories": len(attribution["story_attributions"]),
                "changed_observations": len(attribution["observation_changes"])}
