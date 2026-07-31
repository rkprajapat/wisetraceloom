from trailwise.schema import SCHEMA_VERSION, AgentSpan, EvalScore, LLMSpan, ToolSpan


def test_agent_span_covers_loop_iteration_and_stamps_version():
    span = AgentSpan(
        span_id="s1",
        trace_id="t1",
        agent_id="router-1",
        agent_name="router_agent",
        operation_name="invoke_agent",
        loop_iteration=3,
    )
    assert span.schema_version == SCHEMA_VERSION
    assert span.loop_iteration == 3
    assert span.operation_name == "invoke_agent"


def test_tool_span_defaults_to_function_type():
    span = ToolSpan(span_id="s2", trace_id="t1", tool_name="search")
    assert span.tool_type == "function"
    assert span.success is None


def test_llm_span_covers_token_and_cost_fields():
    span = LLMSpan(
        span_id="s3",
        trace_id="t1",
        provider_name="anthropic",
        request_model="claude-sonnet-5",
        input_tokens=100,
        output_tokens=50,
        cache_read_input_tokens=10,
        estimated_cost_usd=0.0042,
        prompt_version_id="router_agent.system_prompt-v1",
    )
    assert span.input_tokens == 100
    assert span.cache_read_input_tokens == 10
    assert span.estimated_cost_usd == 0.0042
    assert span.prompt_version_id == "router_agent.system_prompt-v1"


def test_eval_score_covers_success_and_threshold_gating():
    score = EvalScore(
        trace_id="t1",
        metric_name="golden_set_pass_rate",
        score=0.97,
        threshold=0.95,
        passed=True,
        prompt_version_id="router_agent.system_prompt-v1",
    )
    assert score.schema_version == SCHEMA_VERSION
    assert score.passed is True


def test_spans_round_trip_through_serialization():
    span = AgentSpan(
        span_id="s1",
        trace_id="t1",
        agent_id="router-1",
        agent_name="router_agent",
        operation_name="plan",
    )
    dumped = span.model_dump(mode="json")
    restored = AgentSpan.model_validate(dumped)
    assert restored == span
