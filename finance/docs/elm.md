# ELM 5.0 transmission: own Swissdec certification or a certified route

Decision document for sending the Swiss salary report (ELM, Lohnstandard-CH) ourselves
from ERPNext/HRMS, the way `hrms.md` leaves it open (Gap: ELM). It answers what the
certification costs, how long it takes, whether a self-built payroll can be certified at
all, and which alternatives keep ERPNext as the calculator. Benchi decided on 2026-10-10:
report ourselves, and look into the certification. **Nothing is built.** The running stack
is unchanged.

Public facts only (Swissdec, BFS, public product pages). No company or employee data, no
insurer or fund names of the company, no rates from policy sheets. Those stay in `private/`.
Figures are as published on 2026-10-10 and are not a quote.

## Verdict

**Self-certification is not a realistic path for us at this stage. Send ELM through a
Swissdec-certified product, and confirm in writing that one accepts our salary data.**

- ELM transmission needs a Swissdec-certified payroll program. Nothing in the public
  sources lets an uncertified payroll send ELM. Swissdec's own pages say so for the
  transmitter side (see "Who may transmit").
- The certification contracts are signed by ERP manufacturers. A company that builds its
  own payroll for its own use does not fit that description, and no public source says
  whether Swissdec would accept it. This is the first question to put to Swissdec.
- If Swissdec does accept it, the first certification takes about 12 months and costs
  roughly CHF 20,500 in the first year plus CHF 4,500 a year. Our payroll would not be
  reportable through ELM before the 2027 cycle.
- No open-source transmitter we found is certified (see "Alternatives").

## Facts that are settled

| Item | Fact | Source |
| --- | --- | --- |
| ELM 4.0 end (tax, Quellensteuer) | Last transmission 31 March 2026, only for the 2025 declaration year. The page also says a software deprecation warning shows 31.12.2025; the page does not explain the difference. Treat 31.03.2026 as the date Swissdec publishes | [Swissdec, ELM 4.0 shutdown](https://www.swissdec.ch/abschaltung-elm-4-0) |
| ELM 4.0 end (other domains: AHV/FAK, UVG, UVGZ, KTG, statistics) | Last transmission 30 June 2026. **Already passed.** Every ELM report now needs ELM 5.0 or higher | same |
| ELM 5.0 requirement | "Für das Abrechnungsjahr 2026 (Löhne ab 1. Januar 2026) ist zwingend ELM 5.0 oder höher erforderlich." | same |
| Current certification basis | ELM 6.0, dated 06.03.2026 ("aktuelle Zertifizierungsbasis"). ELM 5.1 is needed for the voluntary AHV-exemption waiver, ELM 5.3 for FR cross-border workers (mandatory from 2027) | [Swissdec, ELM](https://swissdec.ch/elm), [shutdown page](https://www.swissdec.ch/abschaltung-elm-4-0) |
| Actors | Transmitter (the payroll program), distributor (forwards the files to each receiver), receivers: AHV/FAK, UVG, BVG, KTG, Quellensteuer (cantonal tax offices), BFS statistics. The distributor forwards only what each receiver is entitled to | [BFS, ELM](https://www.bfs.admin.ch/bfs/de/home/grundlagen/elm.html) (title only in the fetch; the actor list is from the search summary, not opened in full); [Swissdec, user ELM](https://swissdec.ch/user-elm) |
| Who may transmit | Swissdec's start steps for a company: "a Swissdec-certified payroll system" and internet access. Swissdec does not track which companies use which version; users ask their software vendor | [Swissdec, user ELM](https://swissdec.ch/user-elm), [shutdown page](https://www.swissdec.ch/abschaltung-elm-4-0) |
| Certified products | 112 product entries from 100 vendors on Swissdec's list, ELM levels 5.0 to 5.5 (no 6.0 column). No entry is open-source or self-built | [Swissdec, certified ERP list](https://swissdec.ch/certified-erp/) |

## Certification: process, cost, time, renewal

**Who certifies.** Swissdec (Verein) certifies payroll programs. Its ERP-manufacturer
page says the target group is "Hersteller einer Lohnsoftware" and that for Swissdec the
ERP manufacturers "handelt es sich dabei immer um Lohnbuchhaltungsprogramme".
([Swissdec, ERP](https://swissdec.ch/erp))

**Contracts** ([Swissdec, certification agreements](https://www.swissdec.ch/certification-agreements), "Zertifizierungsverträge für ERP-Hersteller"; new contracts in force from 1 April 2026):

| Contract | Cost (CHF) | Support hours |
| --- | --- | --- |
| Anschlussvertrag (prerequisite for any standard), per year, 8 years or until the major version at signing expires | 4,500 / year | 10 / year |
| Basisdienste (technical acceptance), one-time | 7,000 | 20 |
| Lohnstandard-CH (ELM), one-time | 9,000 | 40 |
| Pauschalvertrag (flat rate), per year, 4 years, starting 1.4.2026 (together with the affiliation contract, 11,000 / year) | 6,500 / year | 110, freely split |

Figures from the page. My arithmetic, not a quote:

- Light model (affiliation + Basisdienste + ELM): 4,500 + 7,000 + 9,000 = **20,500 in year one**, then 4,500 a year.
- Pauschal model: (4,500 + 6,500) x 4 years = **44,000 over four years**.
- A certification contract that is not finished in its fixed term needs a new one; the
  page gives a CHF 2,000 administration fee for the Pauschal route. Unused support hours
  expire at the end of the term.

**Time.** "Erfahrungsgemäss muss bei einer ersten Zertifizierung mit 12 Monaten gerechnet
werden." ([Swissdec, FAQ ERP](https://www.swissdec.ch/faq-erp)). With a start now, the first
certificate would come around autumn 2027.

**Process** (Swissdec's three areas, [Swissdec, certification](https://swissdec.ch/certification)):
Beratung (analysis and implementation support), Erstzertifizierung (test against the
directives), Re-Zertifizierung (upkeep). After signing, the vendor gets "Swissdec lab"
tools and examples (FAQ). Test cases and the reference application are in the directives
and the lab, not on the public pages; we have not seen them.

**Renewal.** A major version is valid for 8 years; a new major version comes every 4 years.
Minor versions need only proof from the vendor, not a full certification. Re-certification
is needed when the law changes or the software changes substantially. *Source: a Swissdec
release-concept document as summarised in a search result; not opened in full.*

**Costs are the vendor's.** Swissdec's page says costs for the company are included in the
licence of a certified program; the vendor carries certification costs
([Swissdec, certification FAQ summary, via search](https://www.swissdec.ch/swissdec)).

## Can a self-built payroll be certified?

**Not shown by any public source.** The contracts, the FAQ and the certified list all
speak of ERP manufacturers and payroll programs. None mentions a company's own in-house
payroll, and none mentions open source. Our payroll would be an in-house program with a
few users, not a product sold to others. Swissdec may refuse that on the contract's
terms, or may ask for the same agreements and fees. We cannot tell which until Swissdec
answers in writing.

**Frappe/ERPNext has no certified ELM route.** A 2018 forum thread asks whether ERPNext
could be Swissdec-compatible; it names an XML generator as the missing piece and has no
replies from Frappe staff ([discuss.frappe.io](https://discuss.frappe.io/t/swissdec-compatibility/37676)).
erpnextswiss's README lists the Seco reports and the pain.001 payment file, and no ELM
or Swissdec transmission ([libracore/erpnextswiss](https://github.com/libracore/erpnextswiss)).

## Alternatives that keep ERPNext as the calculator

1. **A certified payroll product as the transmitter.** It takes our salary data, does the
   ELM transmission and keeps ERPNext as the calculator. Not verified: we have not found a
   Swissdec-certified vendor that offers to receive a foreign payroll's data for ELM. This
   is the route to ask about first, with written answers on data format, cost and the
   receivers it covers ("all of them": AHV/FAK, UVG, BVG, KTG, tax, statistics).
2. **Move the payroll to a certified product.** Safe, and the standard route in Switzerland.
   It conflicts with Benchi's decision to run full HRMS on ERPNext, so it is a decision for
   Benchi, not a choice this doc makes.
3. **Open-source transmitter** `wap-sarl/swissdec-transmitter` (Apache-2.0; About text
   "Open-source SwissDec Transmitter compliant ELM 6.0"; pushed 2026-08-26; one commit,
   no releases, no README on the page; [GitHub](https://github.com/wap-sarl/swissdec-transmitter)).
   It is **not on Swissdec's certified list**. "Compliant" is the vendor's own word and does
   not mean certified. Using it to send live ELM would mean sending company payroll through
   an uncertified, unreviewed transmitter. We do not recommend it.
4. **Receivers' own portals or forms** for the domains that accept them. Old sources say
   a fund's partner portal takes an XML upload (from 2008, not current). Not verified for
   2026 for any receiver. Check per receiver.
5. **Cantonal web tools for Quellensteuer.** Solothurn's tax office describes an online
   withholding-tax return that works outside ELM. Not verified for other cantons. This is
   the only receiver where a non-ELM route is documented so far.

## Recommendation

1. **Do not build our own Swissdec certification now.** It costs at least CHF 20,500 in
   year one plus CHF 4,500 a year, takes about 12 months, and rests on a contract Swissdec
   offers to ERP manufacturers. Whether a company's own payroll qualifies is unknown.
2. **Write to Swissdec** (info@swissdec.ch, or the contact form) with the question: can a
   company certify its own payroll program for its own use, on what contract, and what
   would it cost? Their answer decides whether the certification route exists at all.
3. **Ask two or three certified payroll vendors** (the list at swissdec.ch/certified-erp)
   whether they transmit ELM for a payroll they do not calculate, for what price, and for
   which receivers. Get the answer in writing.
4. **Decide the calculator after the answers.** If a certified transmitter takes our data,
   keep ERPNext and buy that. If none does, the choice is alternative 2 (a certified
   payroll as the calculator) against the certification route, and that is Benchi's call.
5. Do not use the open-source transmitter for live reports.

## Questions for Benchi

1. **Calculator.** Keep ERPNext/HRMS as the calculator and buy the ELM transmission from a
   certified vendor if one takes our data? Or move the payroll to a certified product?
   (Nothing is built until this is decided.)
2. **Budget.** Is CHF 20,500 in year one plus CHF 4,500 a year, and about 12 months of
   our own effort, acceptable for the certification route, if Swissdec accepts it? The
   Pauschal model is CHF 44,000 over four years.
3. **Timing.** When does ERPNext run the live payroll? A certification started now is
   ready around autumn 2027, so 2026 wages would have to be reported some other way.
4. **2026 reports.** How are the 2026 wage reports sent until ELM is in place? Is the
   payroll still on bexio for 2026?
5. **Receivers.** Which receivers accept a non-ELM submission in 2026 and 2027? The
   Ausgleichskasse, the insurers, the BVG fund and the tax office should be asked
   directly. Names stay in `private/`.
6. **Swissdec contact.** May we write to Swissdec on the company's behalf, and does Benchi
   want the answer as a formal opinion before any contract is signed?

## Sources

- [Swissdec, ELM 4.0 shutdown](https://www.swissdec.ch/abschaltung-elm-4-0): end dates, ELM 5.0 requirement
- [Swissdec, ELM](https://swissdec.ch/elm): ELM 6.0 as current basis (06.03.2026), version history
- [Swissdec, user ELM](https://swissdec.ch/user-elm): company start steps, certified system requirement
- [Swissdec, ERP](https://swissdec.ch/erp): target group of the certification
- [Swissdec, certification](https://swissdec.ch/certification): process areas, certificate meaning
- [Swissdec, certification agreements](https://www.swissdec.ch/certification-agreements): contract fees and terms
- [Swissdec, FAQ ERP](https://www.swissdec.ch/faq-erp): 12 months, "Swissdec lab" tools, validity
- [Swissdec, certified ERP list](https://swissdec.ch/certified-erp/): 112 entries, levels 5.0 to 5.5
- [BFS, ELM](https://www.bfs.admin.ch/bfs/de/home/grundlagen/elm.html): the wage standard (title only in the fetch)
- [discuss.frappe.io, Swissdec compatibility (2018)](https://discuss.frappe.io/t/swissdec-compatibility/37676)
- [libracore/erpnextswiss](https://github.com/libracore/erpnextswiss): feature list, no ELM
- [wap-sarl/swissdec-transmitter](https://github.com/wap-sarl/swissdec-transmitter): uncertified open-source transmitter
- Related: `finance/docs/hrms.md` (Gap: ELM), `finance/docs/swiss.md`

Not opened in full: the BFS page (the fetch returned the title only), the Swissdec
directives (ELM 6.0 and the data fields per domain), the test-case catalogue, and the
Swissdec lab tools. Each of these is needed before any build, and each is a question for
Swissdec, not a fact in this doc.
