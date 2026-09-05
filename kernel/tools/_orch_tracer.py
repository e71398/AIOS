import sys, json
sys.path.insert(0, '${AIOS_HOME}/kernel/tools')
import aios_tool_failover, aios_routing_policy
out = {
    "engine_singleton_alive": aios_tool_failover._ENGINE_SINGLETON is not None,
    "routing_singleton_alive": aios_routing_policy._ROUTING_SINGLETON is not None,
}
if aios_tool_failover._ENGINE_SINGLETON is not None:
    e = aios_tool_failover._ENGINE_SINGLETON
    s = e.compute_tool_status("minimax-official")
    out["minimax-official_status"] = s.status
    out["minimax-official_primary"] = s.primary_binding
    out["minimax-official_verified"] = list(s.verified_bindings)
    out["minimax-official_failure_event"] = e.get_tool_runtime_failure("minimax-official")
    out["minimax-official_runtime_alive"] = e.is_tool_runtime_alive("minimax-official")
    out["_policies_keys"] = sorted(e._policies.keys())
print(json.dumps(out, indent=2, default=str))
