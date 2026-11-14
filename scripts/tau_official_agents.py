"""Thin official-tool adapter for the shared kernel; old protocols stay frozen."""
from __future__ import annotations
import json
from tau2.agent.llm_agent import LLMAgent
from tau2.data_model.message import AssistantMessage, MultiToolMessage, ToolCall as OfficialCall
from tau2.utils.llm_utils import generate
from aftercare.agent_kernel.contracts import StepProposal, ToolCall, ToolDefinition, ToolKind, architectures
from aftercare.agent_kernel.engine import ExecutionKernel
from aftercare.agent_kernel.retail_guards import RetailGuards
from aftercare.agent_kernel.review import ProposalReviewer
from aftercare.agent_kernel.experts import ExpertService
import tau_official_agents_legacy as legacy

ARCHITECTURES=legacy.ARCHITECTURES
AUTH_TOOLS=legacy.AUTH_TOOLS
PRIVATE_READ_TOOLS=legacy.PRIVATE_READ_TOOLS
_authenticated_scope=legacy._authenticated_scope
WRITE_TOOLS=frozenset({'cancel_pending_order','exchange_delivered_order_items','modify_pending_order_address',
    'modify_pending_order_items','modify_pending_order_payment','modify_user_address','return_delivered_order_items'})


def definitions(tools):
    result=[]
    for tool in tools:
        kind=(ToolKind.AUTH if tool.name in AUTH_TOOLS else ToolKind.WRITE if tool.name in WRITE_TOOLS
              else ToolKind.HANDOFF if tool.name=='transfer_to_human_agents' else ToolKind.READ)
        schema=tool.openai_schema['function']
        result.append(ToolDefinition(name=tool.name,kind=kind,parameters=schema['parameters'],description=schema.get('description','')))
    return tuple(result)


class OfficialModelPort:
    def __init__(self, tools, llm_args):
        self.tools={tool.name:tool for tool in tools}
        self.llm_args=llm_args or {}
        self.last_response=None

    def generate(self, *, role, assignment, messages, tools, stage):
        from tau2.data_model.message import SystemMessage, UserMessage, ToolMessage
        converted=[]
        for m in messages:
            if m['role']=='system': converted.append(SystemMessage(role='system',content=m['content']))
            elif m['role']=='user': converted.append(UserMessage(role='user',content=m['content']))
            elif m['role']=='tool': converted.append(ToolMessage(role='tool',id=m['tool_call_id'],content=m['content']))
            else:
                calls=[OfficialCall(id=c['id'],name=c['function']['name'],arguments=json.loads(c['function']['arguments'])) for c in m.get('tool_calls') or []]
                converted.append(AssistantMessage(role='assistant',content=m.get('content'),tool_calls=calls or None))
        kwargs={**self.llm_args,'reasoning_effort':assignment.reasoning,'aftercare_role':role}
        reply=generate(model=assignment.model,messages=converted,
            tools=[self.tools[t.name] for t in tools],call_name=stage,**kwargs)
        if role=="executor":self.last_response=reply
        return StepProposal(text=reply.content or '',calls=tuple(ToolCall(id=c.id,name=c.name,arguments=c.arguments) for c in reply.tool_calls or []))


def public_messages(messages):
    result=[]
    for m in messages:
        row={'role':m.role,'content':m.content}
        if m.role=='tool':
            row['tool_call_id']=m.id
            row['error']=getattr(m,'error',False)
        if getattr(m,'tool_calls',None):
            row['tool_calls']=[{'id':c.id,'type':'function','function':{'name':c.name,'arguments':json.dumps(c.arguments)}} for c in m.tool_calls]
        result.append(row)
    return result


class KernelRetailAgent(LLMAgent):
    def __init__(self, *, architecture, **kwargs):
        super().__init__(**kwargs)
        self.port=OfficialModelPort(self.tools,self.llm_args)
        # Specialists get no read port here: every tool call stays in the official trajectory.
        self.kernel=ExecutionKernel(architectures()[architecture],self.port,guard=RetailGuards(),
            reviewer=ProposalReviewer(self.port),experts=ExpertService(self.port))
        self.private_trace=[]

    def _generate_next_message(self, message, state):
        state.messages.extend(message.tool_messages if isinstance(message,MultiToolMessage) else [message])
        try:
            proposal=self.kernel.step(public_messages(state.messages),definitions(self.tools),self.domain_policy,
                historical_prompt=self.system_prompt if self.kernel.architecture.name=='S0' else None)
        finally:
            # Preserve the failed step and review resolution on guard/budget/provider errors.
            self.private_trace=[event.model_dump(mode='json') for event in self.kernel.trace]
        # Preserve upstream timing/usage/raw data while applying kernel-selected action.
        reply=self.port.last_response.model_copy(deep=True)
        reply.content=proposal.text or None
        reply.tool_calls=[OfficialCall(id=c.id,name=c.name,arguments=c.arguments) for c in proposal.calls] or None
        return reply

    @property
    def system_prompt(self):
        if hasattr(self,'kernel') and self.kernel.architecture.name=='S0':
            return super().system_prompt+'\n\n'+legacy.EXECUTION_GUIDANCE
        return super().system_prompt


def create_agent(architecture, tools, domain_policy, llm, llm_args=None, additional_stop_tool_names=frozenset()):
    if architecture in legacy.ARCHITECTURES:
        return legacy.create_agent(architecture,tools,domain_policy,llm,llm_args,additional_stop_tool_names)
    if architecture not in architectures():raise ValueError(f'Unsupported architecture: {architecture}')
    return KernelRetailAgent(architecture=architecture,tools=tools,domain_policy=domain_policy,llm=llm,
        llm_args=llm_args,additional_stop_tool_names=additional_stop_tool_names)
