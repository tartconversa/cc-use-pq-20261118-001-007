# Meridian 区域媒体证据分析服务

本服务供研究团队对本地提供的媒体观察记录进行地区、议题和受众比较。系统不抓取网页、不自动推断报道倾向，也不声称合成演示能代表真实群体。

来源登记绑定地区、媒体和允许词表；观察记录按来源和外部编号保留修订。两名研究员独立编码，分歧由第三人裁定。按地区、故事、议题和受众去重，报告显示分母、排除原因和原始证据数。

使用 `python -m meridian.api --db meridian.sqlite3` 启动 FastAPI。主要接口包括 `POST /sources`、`POST /observations`、`POST /decisions`、`POST /adjudications`、`POST /batches`、`POST /reports` 和 `POST /releases`。采集窗口使用 `/frames`，导出使用 `/reports/export` 或 `/releases/{release_id}/export`。

冻结发布的可复核差异归因使用 `GET /releases/{release_id}/attribution`：以故事（region + story_key）为单元输出进入、退出、合并与倾向改变，同一故事上的多种变化只产生一个净效应，绝不把逐项差值相加冒充总变化。响应包含两版完整分母、逐故事成员与净效应、地区/来源净值、观察修订与审核血缘、与倾向分析分开报告的采集覆盖漂移，以及可与两版报告实际差值逐项对账的 `reconciliation` 块。`GET /releases/{release_id}/attribution/export` 输出带 `bundle_hash` 的证据包，可用 `ReleaseAttribution.verify` 核验；`GET /releases/{release_id}/observations/{origin}/{external_id}/trace` 追溯单条观察的修订链、当时与当前审核结论和事件流。撤回稿保留在历史证据中，后到转载不改写冻结成员关系。端到端人工复核示例见 `python3 -I review/run_attribution_review.py`。

运行 `python -m unittest discover -s tests -v`、`python -m compileall -q meridian` 和 `python -m meridian.demo`。数据库使用 SQLite；测试和演示使用临时本地数据库。
