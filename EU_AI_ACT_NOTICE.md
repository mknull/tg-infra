# EU AI Act Notice

**Repository:** [`mknull/tg-infra`](https://github.com/mknull/tg-infra)  
**Assessment date:** 2 August 2026  
**Relevant law:** Regulation (EU) 2024/1689 (the **EU AI Act**), as amended, including by Regulation (EU) 2026/1744

> [!IMPORTANT]
> This notice describes the compliance position of the repository **only within its current documented purpose and deployment model**. It is not a certification, warranty, or substitute for legal advice. EU AI Act classification depends on the system's intended purpose, the entity operating it, the people affected by its outputs, and the actual deployment context.

## Current compliance position

As currently documented, this repository is a self-hosted job-market intelligence tool for an individual job seeker. It:

- collects vacancy messages from configured Telegram channels and an optional Outlook inbox;
- uses AI models to evaluate **vacancies**, not job applicants;
- forwards potentially relevant vacancies to the user's private Telegram chat;
- produces optional AI-generated research briefs at the user's request;
- adapts recommendations from the user's own instructions and feedback;
- keeps an audit trail of model decisions; and
- leaves all application and career decisions with the user.

Within that defined use, **no EU AI Act non-compliance has been identified**.

The current system is not intended to recruit or select natural persons on behalf of an employer, rank applicants, determine access to employment, monitor workers, or make decisions about employment relationships. It therefore does not presently appear to fall within the high-risk employment use cases listed in Annex III, point 4, of the EU AI Act.

A natural person operating the system solely for a private, non-professional activity may also benefit from the exclusion for personal use in Article 2(10). The source-code release may benefit from the free and open-source exclusion in Article 2(12), subject to that provision's exceptions. These exclusions are context-dependent and must not be assumed for a public, professional, employer-facing, or commercial deployment.

## Compliance boundary

The present assessment depends on all of the following remaining true:

1. **The system serves the job seeker.** It is not operated by an employer, recruiter, staffing company, employment agency, or similar intermediary to assess people.
2. **The system evaluates vacancies, not candidates.** It does not score, rank, filter, shortlist, reject, recommend, or profile natural persons for employment.
3. **The user retains authority.** The system recommends opportunities but does not make binding employment decisions or submit representations as though they were human decisions.
4. **The system is not used for targeted recruitment advertising.** Employers do not pay or otherwise direct the system to decide which people receive or are denied particular employment opportunities.
5. **AI involvement is disclosed where required.** A person interacting with a public or multi-user deployment is clearly informed, by the first interaction, that AI is being used.
6. **The system does not infer workplace emotions or prohibited sensitive traits.**
7. **The documented purpose is not materially changed.**
8. **Existing audit, security, human-control, and guardrail mechanisms are not represented as substitutes for obligations that would apply if the system became high-risk.**

Small changes to the code, prompts, configuration, user interface, target users, business model, or deployment context can move the system outside this boundary.

## Small changes that could render a deployment non-compliant

### 1. Adding applicant screening

A seemingly small feature allowing an employer or recruiter to upload CVs and receive `accept`, `reject`, `shortlist`, `interview`, suitability scores, or candidate rankings would change the system from vacancy recommendation into recruitment or selection of natural persons.

Examples include:

- replacing vacancy triage with CV triage;
- comparing applicants against a job description;
- ranking applicants by predicted fit;
- recommending whom to interview;
- automatically rejecting applications;
- generating candidate risk, quality, or employability scores; or
- filtering candidates before a human recruiter sees them.

This would likely make the system a **high-risk employment AI system** under Annex III, point 4(a). Deploying it without satisfying the applicable high-risk requirements could be non-compliant.

### 2. Giving the system to recruiters or employment agencies

The same underlying matching logic may be classified differently when used by an employment intermediary.

Potentially high-risk changes include:

- allowing recruiters to search for candidates;
- suggesting which candidates should be referred to employers;
- matching job seekers to vacancies on behalf of an employment agency;
- suppressing vacancies from particular candidates;
- prioritising candidates for recruiter attention; or
- using the output to control access to employment services.

A feature does not remain low-risk merely because it is described as a “recommendation.” Its practical effect on access to employment matters.

### 3. Introducing targeted job advertising

The system could cross into the Annex III employment category if employers, advertisers, or platform operators use it to place targeted job advertisements.

Examples include:

- employers paying to have vacancies prioritised for selected users;
- selecting audiences using education, career history, age, inferred interests, or behavioural data;
- deciding which users are shown or not shown a vacancy;
- optimising vacancy delivery for an employer's recruitment outcome rather than the job seeker's interests; or
- selling candidate segments or targeting criteria.

A personal filter configured by a job seeker is materially different from an advertising or recruitment system deciding which people receive employment opportunities.

### 4. Automating employment-agency referrals

A minor workflow change that sends a candidate profile directly to an employer, books an interview based on an AI suitability decision, or automatically refers selected users could materially affect access to employment.

Before implementing such a change, assess whether the system is:

- evaluating a natural person;
- determining whether that person reaches an employer;
- controlling which opportunities the person can access; or
- replacing or materially influencing an employment caseworker's judgment.

If so, the system may be high-risk even if a human can theoretically override it.

### 5. Expanding into worker management

The system could become high-risk under Annex III, point 4(b), if adapted to:

- allocate work based on behaviour, traits, or inferred characteristics;
- monitor employee performance or conduct;
- recommend promotions, disciplinary action, pay changes, or termination;
- evaluate productivity, reliability, loyalty, or engagement;
- generate worker rankings; or
- determine contractual terms or access to shifts.

Renaming such outputs “insights,” “signals,” or “recommendations” does not prevent high-risk classification when they influence employment decisions.

### 6. Adding workplace emotion recognition

Adding voice, video, facial, or behavioural analysis to infer a person's emotions during interviews or work could be prohibited, rather than merely high-risk.

Examples include estimating whether a candidate or employee is:

- nervous;
- enthusiastic;
- honest;
- engaged;
- confident;
- frustrated; or
- culturally compatible

from biometric data or similar signals.

The EU AI Act prohibits emotion inference in workplace contexts except for narrowly defined medical or safety purposes. Such a feature must not be added without specialist legal review.

### 7. Inferring protected or sensitive characteristics

The repository must not be changed to infer or use protected or sensitive characteristics as employment signals.

High-risk or prohibited functionality may include inferring or acting on:

- race or ethnic origin;
- political opinions;
- trade-union membership;
- religious or philosophical beliefs;
- sexual orientation or sex life;
- disability or health status;
- pregnancy;
- age, where used discriminatorily; or
- personality characteristics used as proxies for protected traits.

The same concern applies when apparently neutral variables act as reliable proxies for protected characteristics.

### 8. Removing or obscuring AI disclosure

Article 50 transparency obligations apply to certain AI systems interacting directly with natural persons. A public or multi-user deployment could become non-compliant if it:

- presents the bot as a human career adviser;
- removes notices that recommendations and briefs are AI-generated;
- delays disclosure until after the first interaction;
- uses deceptive names, avatars, or wording that imply a human operator;
- forwards AI-generated analysis without identifying its AI origin where required; or
- makes the disclosure inaccessible or difficult to distinguish.

A README statement alone may be insufficient when users interact only through Telegram or another user interface.

### 9. Publishing AI-generated public-interest material without disclosure

The current weekly reports and job briefs are primarily private user outputs. This assessment may change if the system begins publishing AI-generated text to inform the public about labour-market conditions, employers, layoffs, salaries, discrimination, or other matters of public interest.

Such publication may require clear disclosure unless an applicable exception applies, including qualifying human review or editorial control with an identified person or organisation assuming editorial responsibility.

### 10. Turning the private tool into a public or commercial service

Charging money does not automatically make an AI system high-risk. It can, however, remove assumptions that support the current assessment and create provider or deployer obligations.

Changes requiring reassessment include:

- hosting the bot for other users;
- selling access to employers or recruiters;
- offering it as software as a service;
- using personal data for monetisation;
- providing paid integration, support, or managed operation;
- deploying it within an organisation; or
- marketing it for recruitment, staffing, or worker-management purposes.

A public service must not rely on the private personal-use exclusion. A monetised or operational service must not assume that publication of source code is sufficient to obtain an open-source exclusion.

### 11. Rebranding or materially modifying a high-risk deployment

A party can become the legally responsible provider of a high-risk AI system where it:

- places its own name or trademark on that system;
- substantially modifies an existing high-risk system; or
- changes the intended purpose of a non-high-risk system so that it becomes high-risk.

Therefore, a fork that repurposes this repository for candidate selection cannot rely on the original repository's low-risk assessment. The party making that change may inherit provider obligations under Article 25.

### 12. Entering a high-risk use without implementing the required controls

If any change makes the system high-risk, retaining the current codebase without a full compliance programme would not be sufficient.

Depending on the applicable transitional schedule and operator role, high-risk obligations can include:

- a documented lifecycle risk-management system;
- appropriate data governance and bias controls;
- technical documentation;
- automatic record-keeping and retained logs;
- instructions and information for deployers;
- effective human oversight;
- documented accuracy, robustness, and cybersecurity;
- a quality-management system;
- conformity assessment;
- an EU declaration of conformity and CE marking;
- registration in the relevant EU database;
- post-market monitoring;
- corrective-action and incident-reporting procedures; and
- deployer-side obligations, potentially including a fundamental-rights impact assessment.

The repository's existing audit trail and guardrails are useful engineering controls, but they do not by themselves satisfy this regulatory framework.

## Changes that do not automatically create non-compliance

The following changes are not automatically unlawful, but still require review when they affect purpose, users, data, or outputs:

- changing the underlying language model;
- improving vacancy-ranking quality;
- adding new job sources;
- supporting additional languages;
- adding more user-controlled career preferences;
- generating application drafts for the user's review;
- charging for a job-seeker-only service;
- adding ordinary analytics; or
- increasing automation that does not evaluate other natural persons.

The relevant question is not whether a change is technically small. The question is whether it changes the system's intended purpose, operator role, affected persons, legal effect, transparency, or risk profile.

## Mandatory change-review questions

Before merging or deploying a change that affects AI behaviour, answer the following in writing:

1. **Who operates the changed system?**
2. **Who is evaluated by it: vacancies, the operator, job seekers, applicants, candidates, or workers?**
3. **Whose interests is the system optimising?**
4. **Can an output determine or materially influence access to employment?**
5. **Can an output cause a person to be shortlisted, rejected, referred, hidden, monitored, promoted, disciplined, or dismissed?**
6. **Are employers, recruiters, advertisers, or employment agencies involved?**
7. **Will any natural person interact directly with the system, and are they informed that it is AI?**
8. **Does the system infer emotions, personality, health, disability, or protected characteristics?**
9. **Is AI-generated text being published on a matter of public interest?**
10. **Does the change alter the stated intended purpose or create reasonably foreseeable high-risk use?**
11. **Does the change make a new party the provider, deployer, importer, or distributor?**
12. **Which AI Act and other EU-law controls must be completed before deployment?**

A `yes`, `possibly`, or `unknown` answer to questions 4–10 requires a new legal and technical assessment before release.

## Maintainer rule

Do not describe a modified or downstream deployment as covered by this repository's current compliance position unless it remains wholly within the compliance boundary described above.

In particular, **do not deploy recruiter-side, employer-side, employment-agency, targeted-advertising, applicant-evaluation, or worker-management functionality merely by adding a disclaimer**. Classification is based on intended and actual use, not labels.

## Other applicable law

EU AI Act compliance does not establish compliance with other law. This repository processes or may process career profiles, Telegram content, email content, feedback, and model-generated inferences. Depending on deployment, separate obligations may arise under:

- the General Data Protection Regulation;
- the ePrivacy framework;
- employment and anti-discrimination law;
- consumer-protection law;
- platform and communications rules;
- intellectual-property law; and
- national implementing and enforcement rules.

Data-protection and anti-discrimination review is particularly important before processing data about anyone other than the system's private operator.

## Authoritative references

- [Regulation (EU) 2024/1689 — Artificial Intelligence Act](https://eur-lex.europa.eu/eli/reg/2024/1689/oj/eng)
- [Regulation (EU) 2026/1744 — Digital Omnibus on AI amendments](https://eur-lex.europa.eu/eli/reg/2026/1744/oj/eng)
- EU AI Act Article 2: scope, personal-use exclusion, and open-source exclusion
- EU AI Act Article 3: definitions, including intended purpose
- EU AI Act Article 5: prohibited practices
- EU AI Act Article 6 and Annex III, point 4: high-risk employment systems
- EU AI Act Articles 8–27: high-risk requirements and operator obligations
- EU AI Act Article 25: responsibilities following rebranding, substantial modification, or purpose change
- EU AI Act Article 50: transparency obligations

---

**Summary:** The repository's current compliance position is narrow but defensible: it assists an individual in evaluating vacancies for their own use. Small changes that cause it to evaluate people, control access to employment, support employers or intermediaries, target job advertising, monitor workers, infer workplace emotions, or conceal AI interaction can change its legal classification and may make deployment non-compliant unless the corresponding obligations are met.
