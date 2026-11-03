"""dsl_core：中立共享执行侧（两个产品共用）。

backend（规则主导）与 llm-first（LLM 主导）都 import 本包、互不 import 对方：
本包是「执行侧」的单一事实源——DSL 注册表、确定性校验器、编译器、端到端 runner，
以及共享数据模型（PlanningRequest / part_catalog / objective_catalog）。

规则侧理解层（llm_parser / planning_intent 的编译器）留在 backend，不属于本包。

依赖主项目求解内核：maintenance-milp-modeler/scripts（product_data/product_planner）。
"""
