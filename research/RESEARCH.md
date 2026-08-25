# Research: PT Meco Inoxprima market-news monitoring

Research date: 24 August 2026 (Asia/Jakarta)

## Executive conclusion

An exact-name alert is insufficient. PT Meco Inoxprima is a private industrial manufacturer with sparse direct media coverage, while the commercial signals that matter appear under customers, projects, regulators, and product categories. The practical bot is therefore a **market-intelligence monitor**: exact MECO mentions receive the highest score, but the daily pool also covers projects, tenders, expansions, rules, and asset risks that can affect demand for MECO equipment.

A live validation run collected 134 current feed items and selected five useful, non-identical signals: gas-infrastructure construction, a bio-methanol value chain, a new gas delivery point, aviation-fuel logistics, and risk at an LNG facility involving a condensate tank. The exact MECO query returned zero stories in the same rolling week. That is strong evidence for thematic coverage.

## Company and product map

MECO says it was founded in 1978 and designs/manufactures compact turnkey equipment, integrated plants, and industrial equipment. It also engineers plant layouts, erects or modifies plants, and fabricates stainless- and mild-steel parts. Its stated customer industries include chemicals, petrochemicals, cosmetics, food and beverage, and agro-industry. Its listed certifications include ASME S and U stamps and TÜV Rheinland. [Official company profile](https://www.meco.co.id/about.html)

The product evidence supports seven watch lanes:

1. **Process equipment and plants.** MECO lists pressure vessels, tanks, filters, heat exchangers, coolers, autoclaves, conveyors, valves, and fittings, serving food/beverage and agro industries such as palm oil, edible oil, sugar, and animal feed. [Official processing-equipment page](https://www.meco.co.id/processing.html)
2. **LPG storage and transport.** The range includes fixed storage tanks plus truck-mounted and semi-trailer transport tanks. [Official LPG page](https://www.meco.co.id/lpg.html)
3. **Liquid-fuel logistics.** MECO describes aluminium liquid-fuel tankers built around UN-ADR rules and Pertamina guidance. [Official aluminium-fuel page](https://www.meco.co.id/aluminium.html)
4. **Aviation fuelling.** MECO offers aircraft refuellers in configurable capacity, flow-rate, chassis, and fuelling-unit combinations. [Official aviation-equipment page](https://www.meco.co.id/commercial.html)
5. **Energy/process infrastructure.** New gas facilities, terminals, delivery points, storage projects, and refinery/process developments can create tank, vessel, fabrication, inspection, or upgrade demand.
6. **Customer-industry capex.** Plant construction and expansion in food/beverage, agro, palm oil, sugar, chemicals, petrochemicals, cosmetics, and pharmaceuticals are buying signals. The company identifies those sectors among domestic and international customers. [Official customer-sector page](https://www.meco.co.id/service.html)
7. **Materials, standards, and regulation.** Stainless steel/aluminium costs, welding and pressure-equipment rules, TKDN/SNI, and certification changes affect bids, margins, and compliance.

## Competitive and ecosystem scan

Publicly visible Indonesian peers with overlapping capabilities include:

- **Puspetindo**, which lists ASME-certified heat-transfer equipment, pressure vessels/process equipment, bulk LPG bullets, and oil/gas storage tanks. [Official capabilities](https://www.puspetindo.com/en/home/service)
- **PT SK Metalindo**, which advertises ASME pressure vessels, storage tanks, and heat exchangers for energy, petrochemical, and process industries. [Official site](https://skmetalindo.com/)
- **PT Arezda Purnama Loka**, an ASME U/R-stamp fabricator of pressure vessels and heat exchangers. [Official factory profile](https://www.arezda.co.id/factory/)
- **PT Intan Prima Kalorindo**, which lists heat exchangers, pressure vessels, boilers, filtration units, and industrial maintenance. [Official site](https://kalorindo.id/)
- **PT Lintech Duta Pratama**, which documents LPG storage tanks and pressure vessels built to ASME/Pertamina requirements. [Product brochure](https://lintech.co.id/files/Brochure%20LPG%20Tank.pdf)
- **Maju Bersama**, which publishes ASME U/U2 tank and pressure-vessel project references. [Official product page](https://www.maju-bersama.com/energy/id/products-services/detail/147/asme-tank)

These are capability peers, not a verified market-share ranking. The bot treats their names as a separate peer-activity lane so certifications, contract wins, partnerships, and capacity changes can be reviewed without confusing them with direct MECO news. In aviation fuel, Air BP-AKR/Dirgantara Petroindo Raya is monitored as an ecosystem operator because its public scope includes aircraft filling depots and aviation fuel trucks. [AKR aviation-fuel business](https://www.akr.co.id/akr-air-bp)

## Source strategy

| Layer | Default role | Why |
|---|---|---|
| Petromindo oil/gas and infrastructure RSS | Primary sector intelligence | High daily volume of Indonesian project, tender, facility, and energy headlines. Petromindo publishes its RSS endpoints on its [about page](https://www.petromindo.com/about). |
| ANTARA economy and East Java RSS | Primary public/local news | Officially documented feeds, national coverage, and local relevance to MECO's Sidoarjo base. See [ANTARA RSS](https://en.antaranews.com/rss) and [ANTARA East Java RSS](https://jatim.antaranews.com/rss/). |
| Google News query RSS | Discovery and gap filling | Finds Indonesian and English coverage across many publishers. It is treated as a secondary discovery mechanism because Google does not provide a stable commercial RSS API contract for custom search. |
| GDELT DOC 2.0 | Optional multilingual fallback | GDELT supports article-list JSON, a rolling recent window, timespan filters, and up to 250 records. It is disabled by default after live public-endpoint rate limiting; it can be re-enabled when operationally appropriate. [GDELT DOC API overview](https://blog.gdeltproject.org/gdelt-doc-2-0-api-debuts/) |
| Official regulator/company domains | Authority checks and query targets | ESDM, Migas, Kemenperin, BPH Migas, Pertamina, and project owners are favored in domain scoring and search queries. |

RSS/search metadata is preferable to brittle full-page scraping. It reduces publisher load, avoids copying article bodies, survives site-layout changes better, and preserves the publisher link. The bot does not bypass logins, paywalls, or robots controls.

## Relevance and quality controls

The ranking model is intentionally explainable rather than opaque:

- direct “PT Meco Inoxprima” mentions receive a large boost;
- product/industry phrases assign one primary watch lane;
- Indonesia context, commercial actions (project, tender, investment, construction, contract, capacity), trusted domains, and recency add weight;
- consumer, job, sports, disaster-noise, and unrelated uses of words such as “mobil tangki” are penalized;
- the customer-industry lane requires industrial context such as plant, capacity, project, production, construction, or investment;
- normalized URLs, headline fingerprints, and near-title clustering remove duplicates;
- SQLite prevents a delivered story from being sent again;
- topic/source caps encourage variety, while the fallback can relax diversity to meet the requested floor without dropping below the relevance threshold.

## “At least five per day” trade-off

There is no honest way to guarantee five *new and truly relevant same-day* articles for a niche private industrial company every day. A hard quota can silently become low-quality spam. The implemented policy is:

1. search a rolling seven-day window;
2. prefer recent, high-score, diverse stories;
3. never repeat a delivered headline;
4. aim for 5–7 items;
5. if fewer than five unsent items pass the quality floor, send what passed and show a coverage warning.

This meets the business intent—consistent awareness—without presenting irrelevant filler as intelligence. After two to four weeks, delivery history should be reviewed to tune phrases and source weights using actual click/usefulness feedback.

## Telegram and operational constraints

Telegram's official flow is to create a bot with `@BotFather` and protect the issued token like a password. The Bot API is HTTPS/JSON; `sendMessage` accepts a target `chat_id` and formatting mode. [Official bot tutorial](https://core.telegram.org/bots/tutorial) and [Bot API reference](https://core.telegram.org/bots/api)

For private delivery, the user must first open the bot and send `/start`; the included `--discover-chat` command then reads the chat ID from updates. For a group or channel, add the bot and grant the permissions required to post.

For reliable daily operation, use an always-on host/container or Windows Task Scheduler with “start when available.” A laptop that is powered off and never wakes cannot deliver on time. Secrets should stay in `.env` or the host's secret manager, never in source control.

## Recommended review after launch

For the first month, record which stories are useful for sales, procurement, operations, or management. Then adjust:

- keywords that created false positives;
- preferred sources and domains;
- the 5–7 item volume;
- whether separate Telegram digests are needed for sales leads versus general market risk;
- customer and competitor names that MECO management considers strategically important but are not visible as text on the public website.
