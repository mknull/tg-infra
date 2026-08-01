# EU AI Act

This repository is compliant with the EU AI Act in its current form.

It is a private job-search tool. It evaluates vacancies for one user, forwards relevant jobs, and produces optional research briefs. It does not evaluate applicants, rank candidates, make hiring decisions, or manage workers.

The important boundary is simple:

> **Jobs are evaluated for a person. People are not evaluated for an employer.**

## Current scope

The current pipeline stays outside the Act's high-risk employment category because:

- the input is a vacancy, not a candidate;
- the output is `send` or `skip` for the user's own inbox;
- the user makes every career and application decision;
- there is no recruiter or employer interface;
- there is no applicant ranking, rejection, or shortlisting;
- there is no worker monitoring;
- there is no employment advertising sold to employers; and
- the private operator already knows the system uses DeepSeek.

No additional EU AI Act controls are required for this deployment.

## Small changes that cross the boundary

### Candidate triage

Adding CV upload, candidate scoring, applicant ranking, automatic rejection, interview recommendations, or shortlist generation turns the system into recruitment AI.

That is a high-risk employment use under Annex III.

### Recruiter mode

Giving recruiters, employers, staffing firms, or employment agencies access to the matching pipeline changes who the system serves.

A `find candidates for this job` feature is not the inverse of the current bot. It is a different regulated use.

### Targeted job delivery

Letting employers pay to prioritise vacancies for selected users can turn personal filtering into targeted job advertising.

The user must remain the party controlling what is prioritised.

### Worker management

Using the same architecture to score performance, allocate shifts, recommend promotion, detect underperformance, discipline staff, or recommend termination is high-risk employment AI.

### Interview or workplace emotion detection

Adding voice, face, video, or behaviour analysis to infer whether someone is nervous, honest, engaged, confident, or suitable is prohibited in workplace contexts, except for narrow medical or safety uses.

### Protected-trait inference

Do not infer or score race, ethnicity, religion, political opinion, trade-union membership, disability, health, sexual orientation, or similar sensitive traits.

Do not use obvious proxies for them.

### Undisclosed public bot

The current deployment is private. If the bot is opened to other users, they must be told that they are interacting with AI by the first interaction.

Do not present an AI-generated brief or recommendation as the work of a human adviser.

### Automatic applications or referrals

Drafting an application for the user's review does not change the current classification.

Automatically applying, referring a user to an employer, or withholding access to a vacancy based on an AI decision may change it.

## Change check

Before merging a feature, ask:

1. Does it evaluate a person rather than a vacancy?
2. Is an employer, recruiter, agency, or advertiser using the output?
3. Can the output affect access to a job, interview, shift, promotion, or dismissal?
4. Does it infer emotions or sensitive traits?
5. Will another user interact with the bot without being told it is AI?

If every answer is **no**, the feature is probably inside the current boundary.

If any answer is **yes**, reassess the feature before deployment. A disclaimer does not fix a high-risk or prohibited use.

## Rule of thumb

```text
vacancies -> personal recommendation     current scope
people    -> employment decision         regulated scope
```

## References

- [EU AI Act — Regulation (EU) 2024/1689](https://eur-lex.europa.eu/eli/reg/2024/1689/oj/eng)
- Article 5 — prohibited practices
- Article 50 — transparency for AI interaction
- Annex III, point 4 — employment and worker-management systems
