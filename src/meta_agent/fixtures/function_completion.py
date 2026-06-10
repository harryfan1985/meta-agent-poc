"""Running example(设计文档附录 A):function-completion 4-agent swarm。

M0 用 fixture(确定性 handler)模拟四个 agent,验证 Coordinator/ContextStore/
DAG/RuntimeGate 机判全链路。M2 起 cv1 使用受限 python_assert 后端。
也提供错误注入变体,供 M1 归因测试。

设计发现(已反馈到设计文档):附录 A 曾给 spec_analyzer 的
forbidden_patterns=["def ", "return ["],但该节点会**透传 raw_signature**(其值
形如 "def has_close_elements(...)",天然含 "def "),全输出扫描会误命中。这暴露
"forbidden_patterns 扫描透传字段会假阳性"——根因是 forbidden 应**字段限定**或
不扫透传字段。当前 fixture 用 ["```"](禁代码块)规避。
"""
from __future__ import annotations

from ..artifacts import ArtifactLoader
from ..schemas import (
    AgentArtifact,
    AgentSpec,
    AssertionSpec,
    DagEdge,
    ExecutableSwarm,
    FieldSpec,
    IOContract,
    SwarmPlan,
    VerificationCriteria,
    VerificationPolicy,
)

CANDIDATE_CODE = (
    "def has_close_elements(numbers, threshold):\n"
    "    for i in range(len(numbers)):\n"
    "        for j in range(i + 1, len(numbers)):\n"
    "            if abs(numbers[i] - numbers[j]) < threshold:\n"
    "                return True\n"
    "    return False\n"
)

TASK_INPUT_EXAMPLE = {
    "raw_signature": "def has_close_elements(numbers, threshold)",
    "docstring": "Return True if any two numbers are closer than threshold.",
}


# ---------------------------------------------------------------- fixture handlers


def fx_spec_analyzer(message: dict, history: list) -> dict:
    return {
        "raw_signature": message["raw_signature"],  # 显式透传
        "parsed_spec": {
            "inequality_strict": True,
            "edge_cases": ["empty_list", "single_element"],
            "constraints": ["pairwise_compare"],
        },
    }


def fx_algo_planner(message: dict, history: list) -> dict:
    return {
        "approach": {
            "algorithm": "pairwise_compare",
            "ordering_note": "order-independent",
            "preserve_pairing": True,
        }
    }


def fx_code_synthesizer(message: dict, history: list) -> dict:
    return {"candidate_code": CANDIDATE_CODE}


def fx_code_verifier(message: dict, history: list) -> dict:
    return {"final_code": message["candidate_code"], "passed": True}


# 错误注入变体(M1 归因测试用)
def fx_spec_analyzer_drop_inequality(message: dict, history: list) -> dict:
    out = fx_spec_analyzer(message, history)
    del out["parsed_spec"]["inequality_strict"]  # sa1 → local
    return out


def fx_spec_analyzer_drop_passthrough(message: dict, history: list) -> dict:
    out = fx_spec_analyzer(message, history)
    del out["raw_signature"]  # 下游 gather_inputs 缺字段 → structural
    return out


FIXTURES = {
    "fx_spec_analyzer": fx_spec_analyzer,
    "fx_algo_planner": fx_algo_planner,
    "fx_code_synthesizer": fx_code_synthesizer,
    "fx_code_verifier": fx_code_verifier,
    "fx_spec_analyzer_drop_inequality": fx_spec_analyzer_drop_inequality,
    "fx_spec_analyzer_drop_passthrough": fx_spec_analyzer_drop_passthrough,
}


# ---------------------------------------------------------------- plan


def _S(desc: str) -> FieldSpec:
    return FieldSpec(type="string", description=desc)


def _O(desc: str) -> FieldSpec:
    return FieldSpec(type="object", description=desc)


def _B(desc: str) -> FieldSpec:
    return FieldSpec(type="boolean", description=desc)


def build_plan() -> SwarmPlan:
    spec_analyzer = AgentSpec(
        spec_id="spec_analyzer",
        role="把签名+docstring解析成结构化规格,不写代码",
        io_contract=IOContract(
            input_schema={"raw_signature": _S("函数签名"), "docstring": _S("自然语言描述")},
            output_schema={"raw_signature": _S("原样透传"), "parsed_spec": _O("结构化规格")},
            required_in=["raw_signature", "docstring"],
            required_out=["raw_signature", "parsed_spec"],
            description="signature+docstring → parsed_spec",
        ),
        verification_criteria=VerificationCriteria(
            behavioral_assertions=["必须标注阈值比较是否严格不等"],
            machine_assertions=[
                AssertionSpec(
                    assertion_id="sa1",
                    kind="field_present",
                    target_path="/parsed_spec/inequality_strict",
                    description="必须显式给出严格不等标记",
                    failure_subtype="schema_violation",
                ),
                AssertionSpec(
                    assertion_id="sa2",
                    kind="equals_input",
                    target_path="/raw_signature",
                    expected="$input.raw_signature",
                    description="raw_signature 必须原样透传",
                    failure_type="contract",
                    failure_subtype="field_mismatch",
                ),
            ],
            forbidden_patterns=["```"],  # 见模块顶部设计发现:不用 "def "(会误命中透传)
        ),
    )

    algo_planner = AgentSpec(
        spec_id="algo_planner",
        role="据 parsed_spec 选算法与遍历顺序,不写最终代码",
        dependencies=["spec_analyzer"],
        io_contract=IOContract(
            input_schema={"parsed_spec": _O("来自 analyst")},
            output_schema={"approach": _O("algorithm + ordering + preserve_pairing")},
            required_in=["parsed_spec"],
            required_out=["approach"],
            description="parsed_spec → approach",
        ),
        verification_criteria=VerificationCriteria(
            behavioral_assertions=["所选算法不得破坏阈值配对成立条件"],
            machine_assertions=[
                AssertionSpec(
                    assertion_id="ap1",
                    kind="field_present",
                    target_path="/approach/algorithm",
                    description="必须给出算法选择",
                )
            ],
            forbidden_patterns=["import "],
        ),
    )

    code_synthesizer = AgentSpec(
        spec_id="code_synthesizer",
        role="据 spec+approach 写候选实现",
        dependencies=["spec_analyzer", "algo_planner"],
        risk_tier="medium",
        io_contract=IOContract(
            input_schema={
                "raw_signature": _S("透传自 analyst"),
                "parsed_spec": _O("来自 analyst"),
                "approach": _O("来自 planner"),
            },
            output_schema={"candidate_code": _S("完整函数实现")},
            required_in=["raw_signature", "parsed_spec", "approach"],
            required_out=["candidate_code"],
            description="spec+approach → candidate_code",
        ),
        verification_criteria=VerificationCriteria(
            behavioral_assertions=["实现必须使用 analyst 标注的严格不等语义"],
            machine_assertions=[
                AssertionSpec(
                    assertion_id="cs1",
                    kind="regex_match",
                    target_path="/candidate_code",
                    expression=r"def\s+has_close_elements",
                    description="必须定义目标函数",
                )
            ],
            forbidden_patterns=["import os", "subprocess", "open("],  # §7.1 安全红线
        ),
    )

    code_verifier = AgentSpec(
        spec_id="code_verifier",
        role="对照 spec 校验候选代码并定稿",
        dependencies=["spec_analyzer", "code_synthesizer"],
        risk_tier="medium",
        io_contract=IOContract(
            input_schema={
                "candidate_code": _S("来自 synthesizer"),
                "raw_signature": _S("透传自 analyst"),
                "parsed_spec": _O("来自 analyst"),
            },
            output_schema={"final_code": _S("定稿代码"), "passed": _B("是否通过自检")},
            required_in=["candidate_code", "parsed_spec"],
            required_out=["final_code", "passed"],
            description="candidate_code+spec → final_code",
        ),
        verification_criteria=VerificationCriteria(
            behavioral_assertions=["final_code 行为满足 parsed_spec 全部 edge_cases"],
            machine_assertions=[
                AssertionSpec(
                    assertion_id="cv1",
                    kind="python_assert",
                    target_path="/final_code",
                    expression="'def has_close_elements' in value",
                    description="定稿必须含目标函数(受限只读表达式)",
                )
            ],
            forbidden_patterns=["import os", "subprocess"],
        ),
    )

    dag_edges = [
        DagEdge(from_spec="spec_analyzer", to_spec="algo_planner"),
        DagEdge(from_spec="spec_analyzer", to_spec="code_synthesizer"),
        DagEdge(from_spec="algo_planner", to_spec="code_synthesizer"),
        DagEdge(from_spec="spec_analyzer", to_spec="code_verifier"),
        DagEdge(from_spec="code_synthesizer", to_spec="code_verifier"),
    ]

    return SwarmPlan(
        swarm_name="humaneval_has_close_elements",
        summary="signature→spec→approach→code→verified code 的 4 节点严格拓扑序",
        coordination_strategy="严格串行:analyst → planner → synthesizer → verifier",
        specs=[spec_analyzer, algo_planner, code_synthesizer, code_verifier],
        dag_edges=dag_edges,
        verification_policy=VerificationPolicy(risk_tier="medium", allow_model_verification=False),
    )


def build_golden_cases():
    """verifier 校准最小集(§4.7):覆盖 pass / schema_violation / forbidden_hit /
    field_mismatch。M1 用于确认机判 gate 行为符合预期,M3 复用于模型判校准。"""
    from ..schemas import GoldenVerificationCase

    return [
        GoldenVerificationCase(
            case_id="g_pass_verifier", spec_id="code_verifier",
            message={"candidate_code": CANDIDATE_CODE, "parsed_spec": {}},
            output={"final_code": CANDIDATE_CODE, "passed": True},
            expected_ok=True,
        ),
        GoldenVerificationCase(
            case_id="g_schema_violation_analyzer", spec_id="spec_analyzer",
            message={"raw_signature": "def f()", "docstring": "d"},
            output={"raw_signature": "def f()"},  # 缺 parsed_spec
            expected_ok=False, expected_failure_type="spec_adherence",
            expected_subtypes=["schema_violation"],
        ),
        GoldenVerificationCase(
            case_id="g_forbidden_hit_verifier", spec_id="code_verifier",
            message={"candidate_code": CANDIDATE_CODE, "parsed_spec": {}},
            output={"final_code": "import os\n" + CANDIDATE_CODE, "passed": True},
            expected_ok=False, expected_failure_type="spec_adherence",
            expected_subtypes=["forbidden_hit"],
        ),
        GoldenVerificationCase(
            case_id="g_field_mismatch_analyzer", spec_id="spec_analyzer",
            message={"raw_signature": "def f()", "docstring": "d"},
            output={"raw_signature": "WRONG", "parsed_spec": {"inequality_strict": True}},
            expected_ok=False, expected_failure_type="contract",
            expected_subtypes=["field_mismatch"],
        ),
    ]


def build_swarm(analyzer_handler: str = "fx_spec_analyzer") -> ExecutableSwarm:
    """构造并绑定可执行 swarm。analyzer_handler 可换成错误注入变体。"""
    plan = build_plan()
    artifacts = {
        "spec_analyzer": AgentArtifact(
            spec_id="spec_analyzer", implementation_kind="fixture",
            handler_ref=analyzer_handler, passed=True,
        ),
        "algo_planner": AgentArtifact(
            spec_id="algo_planner", implementation_kind="fixture",
            handler_ref="fx_algo_planner", passed=True,
        ),
        "code_synthesizer": AgentArtifact(
            spec_id="code_synthesizer", implementation_kind="fixture",
            handler_ref="fx_code_synthesizer", passed=True,
        ),
        "code_verifier": AgentArtifact(
            spec_id="code_verifier", implementation_kind="fixture",
            handler_ref="fx_code_verifier", passed=True,
        ),
    }
    swarm = ExecutableSwarm(plan=plan, artifacts=artifacts)
    return ArtifactLoader(fixture_registry=FIXTURES).bind(swarm)
