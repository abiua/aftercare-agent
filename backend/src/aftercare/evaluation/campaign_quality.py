"""Bound independent judgments to the original visible input before scoring."""
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class IndependentJudgment(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    source_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    reviewer: str = Field(min_length=1, max_length=120)
    blind_review_record: str = Field(min_length=1, max_length=200)
    judged_before_exposure: bool
    new_information: bool
    acceptable: bool
    proposal_quality: Literal['CORRECT','INCORRECT','NO_PROPOSAL']
    rule_intercepted: bool
    upstream_error: bool
    corrected: bool
    key_fact_omissions: int = Field(ge=0)
    policy_errors: int = Field(ge=0)
    citation_errors: int = Field(ge=0)

    @model_validator(mode='after')
    def correction_needs_error(self):
        if self.corrected and not self.upstream_error:
            raise ValueError('CORRECTION_REQUIRES_PRIOR_ERROR')
        return self


def quality_metrics(rows):
    labels=[]
    new_information=0
    unblinded=0
    for row in rows:
        if row.get('independent_judgment') is None:
            continue
        review=IndependentJudgment.model_validate(row['independent_judgment'])
        if review.source_sha256 != row.get('source_sha256'):
            raise ValueError('REVIEW_INPUT_CHANGED')
        if review.new_information:
            new_information += 1
            continue
        if not review.judged_before_exposure:
            unblinded += 1
            continue
        labels.append(review)
    def ratio(values):
        return sum(values)/len(values) if values else None
    accepted=sum(x.acceptable for x in labels)
    fully_reviewed=len(labels)==len(rows) and bool(rows)
    priced=all(r.get('cost_usd') is not None for r in rows)
    total=sum((Decimal(r['cost_usd']) for r in rows),Decimal(0)) if priced else None
    return {
        'independent_review_status':'COMPLETE' if fully_reviewed else 'PARTIAL' if labels else 'PENDING',
        'independent_review_count':len(labels),'new_information_reviews':new_information,
        'unblinded_reviews':unblinded,
        'independent_success_rate':ratio([x.acceptable for x in labels]),
        'cost_per_independent_success_usd':str(total/accepted) if fully_reviewed and priced and accepted else None,
        'missed_error_rate':ratio([not x.rule_intercepted for x in labels if x.proposal_quality=='INCORRECT']),
        'false_block_rate':ratio([x.rule_intercepted for x in labels if x.proposal_quality=='CORRECT']),
        'correction_rate':ratio([x.corrected for x in labels if x.upstream_error]),
        'key_fact_omissions':sum(x.key_fact_omissions for x in labels) if labels else None,
        'policy_errors':sum(x.policy_errors for x in labels) if labels else None,
        'citation_errors':sum(x.citation_errors for x in labels) if labels else None,
    }
