#!/usr/bin/env bash
set -euo pipefail

uv run vulture \
  packages apps src \
  --exclude "*/tests/*,*/.venv/*,*/meeseeks_ha_conversation/*,apps/meeseeks_cli/.venv/*" \
  --ignore-decorators "REGISTRY.command,@validator,@app.before_request,@ns.route" \
  --ignore-names "_cmd_*,list_commands,langfuse_reason,Config,goal,plan,drop_ids,ts,entity_ids,sensor_ids,services,sensors,allowed_domains,sensor,input_data,discover_mcp_tools,load_recent_events,list_sessions,list_tags,select_many,CSS,BINDINGS,compose,action_*,on_*,arbitrary_types_allowed,HomeAssistant"
