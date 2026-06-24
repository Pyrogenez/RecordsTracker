"""Florida Public Records Law reference material.

Injected as the base system-prompt context on every AI call so the model has
immediate access to the statutory framework, key AG opinions, and landmark
case law when analyzing a request.

This is a working legal reference, not legal advice. It should be kept
up to date as statutes and case law evolve. Sources include Fla. Stat.
Chapter 119, the Government-in-the-Sunshine Manual (published annually by
the Florida Attorney General), and published appellate decisions.
"""
from __future__ import annotations


# Hand-maintained: the vintage of the statutory text and case law below. Bump
# this when the reference is reviewed/updated (it is decoupled from app version).
REFERENCE_AS_OF = "2024 (Fla. Stat. Ch. 119 + Government-in-the-Sunshine Manual)"


CHAPTER_119_REFERENCE = """\
# FLORIDA PUBLIC RECORDS LAW — REFERENCE FOR AI ANALYSIS

## CONSTITUTIONAL FOUNDATION
- Fla. Const. art. I, § 24 (Access to Public Records and Meetings) guarantees
  the public a right of access to the records of all three branches of state
  government and all political subdivisions, subject to narrow exemptions
  that must be passed by 2/3 of each house of the Legislature and must state
  a specific public necessity.

## KEY STATUTES — CHAPTER 119 (Public Records)

### § 119.01 — General state policy
All state, county, and municipal records are open for personal inspection
and copying by any person. Providing access to public records is a duty of
every agency. Electronic records are public records and must be provided
in the form requested if the agency maintains them in that form.

### § 119.011 — Definitions
- "Public record" means all documents, papers, letters, maps, books, tapes,
  photographs, films, sound recordings, data processing software, or other
  material, regardless of physical form or characteristics, made or received
  pursuant to law or ordinance, or in connection with the transaction of
  official business by any agency.
- "Agency" includes every state, county, district, authority, or municipal
  officer, department, division, board, bureau, commission, and any other
  public or private agency, person, partnership, corporation, or business
  entity acting on behalf of a public agency.
- "Custodian of public records" is the elected or appointed state, county,
  or municipal officer charged with maintenance of records, or their
  designee.

### § 119.07(1) — Inspection and copying
(a) Every custodian shall permit the record to be inspected and copied by
    any person desiring to do so, AT ANY REASONABLE TIME, under reasonable
    conditions, and under supervision by the custodian.
(b) A custodian who asserts an exemption shall state in writing and with
    particularity the reasons for the conclusion that the record is exempt
    or confidential, upon request.
(c) If the nature or volume of records requires extensive use of information
    technology resources or extensive clerical or supervisory assistance,
    the agency may charge, in addition to the actual cost of duplication,
    a SPECIAL SERVICE CHARGE which shall be reasonable and based on the
    cost actually incurred by the agency.
(d) If a record contains both exempt and non-exempt information, the
    custodian shall redact ONLY the exempt portion and produce the rest.
(f) A failure to respond to a request to inspect or copy the record
    CONSTITUTES A VIOLATION of Chapter 119.

### § 119.07(4) — Fees
(a) Duplication cost may not exceed 15 cents per one-sided copy, 20 cents
    per two-sided copy for copies 14 inches by 8.5 inches or less.
(a)3. If a record contains information that is exempt or confidential,
    the custodian may NOT charge for time spent reviewing the record for
    exemptions UNLESS authorized by another statute.
(d) The special service charge under § 119.07(1)(c) may only cover the
    labor cost of personnel actually required to fulfill the request;
    charges for supervision, training, or administrative overhead are
    generally not permitted. The charge must reflect the lowest-paid
    employee capable of doing the work.

### § 119.071 — Statutory exemptions
Contains most categorical exemptions. Common ones relevant to city records:
  - 119.071(2)(c)1 — active criminal intelligence/investigative information
  - 119.071(2)(d) — criminal investigations, portions
  - 119.071(2)(h) — home addresses/phone numbers of law enforcement,
    firefighters, corrections officers, judges, and other protected classes
  - 119.071(4)(d) — personal info of active/former sworn/civilian law
    enforcement personnel, family members
  - 119.071(1)(d) — attorney work product and attorney-client
    communications concerning pending litigation
  - 119.071(3)(a) — building plans (security-sensitive portions)

### § 119.0701 — Contracts with public agencies / contractor obligations

### § 119.10 — PENALTIES / CIVIL AND CRIMINAL LIABILITY
(1)(a) A public officer who VIOLATES any provision of this chapter
    commits a noncriminal infraction, punishable by fine not exceeding $500.
(1)(b) A public officer who KNOWINGLY AND WILLFULLY violates this chapter
    commits a MISDEMEANOR of the first degree.
(2) A person who WILLFULLY AND KNOWINGLY violates § 119.105 (release of
    certain criminal-records info) commits a misdemeanor of the first degree.

### § 119.11 — Accelerated hearing; burden of proof
Whenever an action is filed to enforce the provisions of Chapter 119, the
court shall set an IMMEDIATE HEARING, giving the case priority over other
pending cases. The burden is on the agency to justify any withholding.

### § 119.12 — ATTORNEY'S FEES (amended 2017, 2024)
(1) If a civil action is filed against an agency to enforce Chapter 119
    and the court determines the agency UNLAWFULLY REFUSED TO PERMIT a
    public record to be inspected or copied, the court SHALL assess and
    award against the agency the reasonable costs of enforcement including
    REASONABLE ATTORNEY FEES, if:
    (a) The requester provided written notice at least 5 business days
        before filing the civil action (subject to exceptions), AND
    (b) The agency unlawfully refused to permit inspection or copying.
Note: The 2017 amendment added the pre-suit notice requirement to curb
"gotcha" plaintiff practice; the 2024 amendments further clarified notice
requirements. Always check current statute for latest language.

## RESPONSE-TIME STANDARDS
- Chapter 119 does NOT fix a specific number of days. Courts have instead
  interpreted § 119.07(1)(a) to require response within a "REASONABLE
  TIME" — "only the limited reasonable time allowed the custodian to
  retrieve the record and delete those portions of the record the
  custodian asserts are exempt." Tribune Co. v. Cannella, 458 So. 2d 1075
  (Fla. 1984).
- "Automatic delay" or a blanket policy of delay is unlawful. Michel v.
  Douglas, 464 So. 2d 545 (Fla. 1985).
- A delay that is not justified by the need to retrieve, review, or
  redact the specific record is actionable. Courts routinely find delays
  of weeks or months, without a good-faith explanation, to violate the law.
- "Unjustified delay" and "de facto denial" are treated as denials. If
  the agency never produces the record, it's a denial; if it delays
  unreasonably, it may also be a denial.

## KEY CASE LAW (non-exhaustive but load-bearing)

- **Tribune Co. v. Cannella, 458 So. 2d 1075 (Fla. 1984)** — The leading
  case on response timing. An agency may take "only the limited reasonable
  time" to review and redact. Pretextual or policy-based delay is unlawful.

- **Michel v. Douglas, 464 So. 2d 545 (Fla. 1985)** — Agency policies
  requiring automatic delays before records are produced violate Chapter 119.

- **Board of County Comm'rs of Highlands County v. Colby,
  976 So. 2d 31 (Fla. 2d DCA 2008)** — An agency cannot require a requester
  to state a purpose or identify themselves; and excessive fee estimates
  designed to deter access can violate the statute.

- **Promenade D'Iberville, LLC v. Sundy, 145 So. 3d 980 (Fla. 1st DCA 2014)** —
  An agency must explain in writing, with particularity, the basis for any
  claimed exemption under § 119.07(1)(f).

- **Lilker v. Suwannee Valley Transit Auth., 133 So. 3d 654 (Fla. 1st DCA 2014)** —
  Attorney's fees are mandatory where an agency has unlawfully refused.

- **Grapski v. City of Alachua, 31 So. 3d 193 (Fla. 1st DCA 2010)** —
  Fees must be based on actual cost of personnel actually required; a
  markup for overhead or for unnecessarily senior staff is improper.

- **Board of Trustees v. Lee, 189 So. 3d 120 (Fla. 4th DCA 2016)** —
  Agencies may not impose "inspection fees" for simply viewing records;
  inspection is free.

- **Butler v. City of Hallandale Beach, 68 So. 3d 278 (Fla. 4th DCA 2011)** —
  The custodian's duty is a non-delegable statutory obligation;
  assignment to outside counsel does not excuse compliance.

- **Fulton v. School Board, 987 So. 2d 149 (Fla. 1st DCA 2008)** —
  A records request need not be in any particular form; oral requests
  suffice, and agencies cannot require use of a specific form.

- **National Collegiate Athletic Ass'n v. Associated Press,
  18 So. 3d 1201 (Fla. 1st DCA 2009)** — Records in the custody of a
  third-party contractor acting on behalf of an agency remain public.

## COMMON NON-COMPLIANCE PATTERNS TO WATCH FOR

When auditing a record for Chapter 119 compliance, look for:

1. **Unreasonable delay without explanation** — long gaps between the
   initial auto-ack and a substantive response, with no deadline given,
   no interim status explanation, and no claim of exemption review.
   Cite § 119.07(1)(a) and Tribune Co. v. Cannella.

2. **Blanket denial without particularized exemption** — refusing to
   produce records with a generic "not releasable" or similar language,
   without citing a specific statute and stating, in writing, the basis.
   Cite § 119.07(1)(f) and Promenade D'Iberville.

3. **Excessive or improper fees** —
   - Copy charges above 15¢/20¢ limits for letter/legal size,
   - Special service charges that appear to cover overhead, training,
     supervisory, or attorney review rather than actual labor,
   - Fees for INSPECTION (rather than copies),
   - Fees for reviewing for exemptions (generally improper per § 119.07(4)(a)3),
   - Use of the most senior rather than lowest-qualified staff.
   Cite § 119.07(4) and Grapski v. City of Alachua, Board of Trustees v. Lee.

4. **Demand for requester identification or purpose** — conditioning
   production on who is asking or why. Cite Colby.

5. **Failure to produce electronically where the record exists electronically**
   — forcing paper copies when the requester asked for a digital format and
   the agency maintains the record that way. Cite § 119.01(2)(f).

6. **Redaction without particularity** — redacting portions without
   citing the specific exemption statute and stating the basis.
   Cite § 119.07(1)(d), § 119.07(1)(f).

7. **Requiring use of a specific form or portal as a precondition** —
   Cite Fulton.

8. **Prolonged "pending review" status** with no explanation for what
   is being reviewed, by whom, or on what timeline — courts treat
   sustained non-response as a de facto denial. Cite Tribune Co. v. Cannella.

9. **Ignoring the written-exemption-explanation duty** when the agency
   invokes any exemption. Cite § 119.07(1)(f).

10. **"Clarification" requests used as a stall** — asking for information
    the agency doesn't actually need, or repeatedly asking for the same
    clarification. The Sunshine Manual notes agencies may seek reasonable
    clarification, but cannot use clarification requests as a stalling tactic.

## ANALYSIS GUIDELINES FOR AI

When asked to audit a record for compliance:
- Identify EACH potential issue separately.
- Cite the specific statute section (e.g. "§ 119.07(1)(f)") where possible.
- Cite controlling case law by name where applicable.
- Describe the SPECIFIC EVIDENCE in the record (dates, quotes from messages,
  fee amounts, attachment filenames) that supports the finding.
- Rate severity as low/medium/high based on how clearly the conduct
  departs from the statute.
- Be conservative about calling something "willful" — that requires
  knowing state of mind. Flag as possible but do not assume.
- Distinguish between the conduct of this particular agency staff person
  and systemic agency policy where possible.
- Note when the record is too incomplete to make a determination, and
  what additional information would be needed.

When answering general questions about Florida Public Records Law,
quote statutes and cases accurately; say "I don't know" rather than
invent authority. This reference is not exhaustive — current statutes
and appellate decisions should be consulted for any enforcement action.
"""


def short_system_prefix() -> str:
    """A very short lead-in that just reminds the model of its role.
    Used on the cheaper classification calls where loading the full
    reference would be wasteful."""
    return (
        "You assist a St. Petersburg, FL resident in tracking their public "
        "records requests to the City of St. Petersburg. You have "
        "working knowledge of Florida Public Records Law (Chapter 119 of the "
        "Florida Statutes) and relevant Florida case law. Your statutory and "
        f"case-law reference is current as of {REFERENCE_AS_OF}; flag when an "
        "answer may turn on more recent amendments or decisions, and remember "
        "this is research assistance, not legal advice."
    )


def full_system_prompt(task_description: str = "") -> str:
    """Full system prompt with the Chapter 119 reference loaded. Use for
    compliance audits, chat, and any analysis requiring legal reasoning."""
    header = short_system_prefix()
    if task_description:
        header = header + "\n\n" + task_description.strip()
    return header + "\n\n" + CHAPTER_119_REFERENCE
