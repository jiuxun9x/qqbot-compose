"""Narrow, reviewable context-budget update; no model or credential changes."""
import copy


def with_budget(config, budget=204800):
    result=copy.deepcopy(config)
    result['agent_runner']['config']['compression']['fallback_max_tokens']=budget
    for provider in result.get('provider',[]):
        if provider.get('id') in ('api_chat','api_deep'):
            provider['max_context_tokens']=budget
    return result
