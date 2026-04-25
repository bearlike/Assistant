"""Built-in agent type definitions (like Claude Code's PLAN_AGENT)."""

PLAN_AGENT_CONFIG = {
    "agent_type": "plan",
    "prompt_name": "plan_mode",
    "denied_tools": [
        "spawn_agent",
        "steer_agent",
        "check_agents",
        "exit_plan_mode",
    ],
    "plan_mode_tools": True,
    "max_steps_multiplier": 1,
}
