# Parcels for Home Assistant

Parcels watches a mailbox for shipping emails and keeps a live list of what is on its way: merchant, item, carrier,
status and expected delivery. It reads order and dispatch emails with a rule parser, optionally lets an AI read the
details the rules cannot (item, order lines, delivery window), and optionally follows the parcel with 17TRACK and
Ship24. You confirm reception with one tap, and Home Assistant fires events you can use for notifications.

- Finds parcels from Amazon, Royal Mail, Evri, DHL, DPD, InPost, Yodel, UPS, FedEx and more, including forwarded emails.
- One parcel per shipment: the "ordered", "shipped", "out for delivery" and "delivered" emails (and 17TRACK updates)
  of one parcel are joined by tracking number or order number. Status only moves forward.
- Status, ETA and delivery window, live carrier events and location (with 17TRACK), tracking and order links.
- Flags parcels that need attention: failed delivery, delay, no movement, late, or flagged by you.
- A to-do list (`todo.parcels`), fixed slot sensors for dashboards, buttons, services and `parcels_update` events.
- Optional delivery-address check, so a parcel for someone else's address is not counted as arriving today.
- Works without any AI and without 17TRACK; both only add detail.

## How it works

1. **Email scan.** Every 5 minutes (and once at start) Parcels logs in to the IMAP folder and fetches messages newer than
   the last one it handled. The first run looks back 30 days. It only reads (`BODY.PEEK`) and finally marks processed
   messages as read; it never deletes or moves anything.
2. **Rule parser.** Carrier from the sender and wording, tracking numbers per carrier format, order number, status
   keywords, dates and delivery windows. Shipping emails create or update parcels.
3. **Optional AI pass.** The email text goes to an AI (a Home Assistant AI Task entity or an Ollama server) that returns
   merchant, item, order lines, status, problem, ETA and window as JSON. Its answer is validated (a tracking number must
   appear verbatim in the email, enums and dates must parse) and merged with the rules' result. If the AI is
   unavailable, the parcel appears anyway from the rules and the email is retried when the AI is back ("details pending").
4. **Tracking.** Tracking numbers are added to your 17TRACK account (the carrier is set explicitly when 17TRACK cannot
   guess it) and polled every 15 minutes. With a Ship24 key, parcels handed from an export carrier (4PX, Yanwen...) to a
   local carrier are joined to the local carrier's parcel.
5. **Parcels.** Parcels carry status, ETA and follow-up flags. A "delivered" sticks until you confirm reception.

**Email bodies are never stored.** Only a reference (IMAP UID and Message-ID, a subject line of up to 120 characters
and a short note of what each email changed) is kept in Home Assistant's storage, so emails can be fetched again when
the AI comes back.

## Adapting the rules to your country

The rule parser reads its country and language data (carriers and their sender domains, tracking number formats, tracking
pages, shops, the phrases that mean "shipped" or "delivered", weekday and month names, postcode format...) from plain
data, not from code. The defaults (United Kingdom, English) are in
[`custom_components/parcels/rules.yaml`](custom_components/parcels/rules.yaml), with comments on every section. Do not
edit that file (an update overwrites it). Instead:

1. Create `parcels_rules.yaml` in your Home Assistant configuration folder (next to `configuration.yaml`) and put only your
   additions or changes in it. [`examples/parcels_rules.yaml`](examples/parcels_rules.yaml) is a complete French example.
2. Run the action **Parcels: Reload rules** (Developer tools > Actions). No restart is needed.
3. Try it with **Parcels: Test email** (see below) before waiting for a real email.

How your file is combined with the defaults:

- **Mappings** (`carriers`, `merchants`, `status`, `problems`) are merged by key: your entry wins and the defaults you do
  not mention stay. Set a key to `null` to remove a default entry.
- **Lists** (domains, phrases, regexes) are appended to the default lists.
- A section named in the top-level `replace:` list is replaced by yours instead. Use it for `weekdays`, `months` and
  `postcode` when the language changes, and whenever you want none of the defaults.

```yaml
# <config>/parcels_rules.yaml
replace: [postcode]
postcode: ['\b(\d{5})\b']
carriers:
  colissimo:                          # a new carrier
    name: Colissimo
    domains: [colissimo.fr, laposte.fr]
    tracking: ['\b[A-Z]{2}\d{9}FR\b']
    page: 'https://www.laposte.fr/outils/suivre-vos-envois?code={}'
  royal_mail: null                    # remove a default carrier
status:
  delivered: ['\blivr[ée]e?\b']       # one more phrase for "delivered"
```

Write regular expressions in 'single quotes' so backslashes stay as they are. To try a change, call:

```yaml
action: parcels.test_email
data:
  subject: Votre colis est en cours de livraison
  sender: Colissimo <noreply@colissimo.fr>
  body: "Votre colis 6A12345678901 sera livre demain."
```

The response lists what the rules read: status, problem, carrier, tracking numbers, merchant, order number, expected
date and window, delivery address and links. Nothing is stored.

If a rules file is wrong (broken YAML, a bad regular expression, a wrong type), Parcels shows a **repair** that names the
file, section and entry, and keeps working with the built-in rules (after a reload: with the previous rules). Fix the file
and run **Parcels: Reload rules** again; the repair disappears.

Not configurable: the AI prompt (English; most models cope with other languages), the status names shown on the
dashboards, and the "Forwarded message" markers of a forwarded email.

## Requirements

- Home Assistant 2026.11.0 or newer.
- Home Assistant's own **IMAP** integration, set up for the mailbox (and folder) that receives your shipping emails.
  A dedicated mailbox that only receives shipping mail is a good idea (see Privacy).
- Optional: a free [17TRACK](https://www.17track.net) account (live carrier status).
- Optional: an AI, either any entity of Home Assistant's **AI Task** integration or an **Ollama** server with a model
  pulled (default `qwen3:30b`).
- Optional: a free [Ship24](https://www.ship24.com) API key.

Parcels installs its own `pyseventeentrack` library; you do not need the 17TRACK or Ollama integrations.

## Installation

Needs Home Assistant 2026.11 or newer.

**From the Marketplace (recommended).** Parcels is not in the Marketplace catalog yet, so add it from its link:

1. Settings > **Marketplace** > **Add from link** (upper right). The first time, Home Assistant asks you to connect a
   GitHub account; that is needed for anything added from a link.
2. Under **GitHub link** enter `https://github.com/debarrylt/ha-parcels` and select **Add**. If asked for a type, pick
   **Integration**.
3. Open **Parcels** in the Marketplace, select **Install**, check the version and select **Install** again.
4. Restart Home Assistant when the repair under Settings > System > Repairs asks you to.

Updates appear under Settings > **Updates** like any other update (restart after updating). To go back to an older
version: the Parcels page in the Marketplace > three-dot menu > **Install another version**. To remove it: three-dot
menu > **Uninstall**.

**Manual.** Copy `custom_components/parcels` into your Home Assistant `config/custom_components/` folder and restart.
You won't get update notices this way.

Then set up in two steps:

1. Settings > Devices & services > Add integration > **IMAP**. It validates the login and the folder, and its sensor
   shows the unread count, so you know mail access works before Parcels uses it.
2. Add integration > **Parcels** and pick that IMAP entry. Parcels reads the folder set in the IMAP entry.

## Configuration

The setup wizard has these pages. Everything is checked before you can continue.

| Page | Fields | Checked by |
|---|---|---|
| **Mailbox** | A dropdown of your IMAP entries | The entry must be loaded; Parcels connects through it (15 s timeout). Errors: IMAP entry not loaded, cannot connect. Without any IMAP entry the setup stops and asks you to add one. |
| **Tracking** (optional) | 17TRACK email and password, Ship24 API key | Logs in to 17TRACK the way Home Assistant's own 17TRACK integration does. A new or changed Ship24 key is tested against Ship24 (`GET /couriers`, uses no tracker quota): invalid key, or Ship24 unreachable. Leave empty to skip. |
| **AI** | None (rules only), AI Task entity, or Ollama | - |
| **AI Task entity** | Any `ai_task` entity | - |
| **Ollama server** | URL, model (default `qwen3:30b`), optional API key | `GET {url}/api/tags` must answer and list the model. |
| **Home address** (optional) | Home postcode, home names (comma-separated) | - |
| **Orders without a tracking number** (optional) | On/off (default off), first check after (3 days), check again every (3 days), stop after (30 days) | Stop after must not be before the first check. |

The mailbox login stays in the IMAP integration. The chosen IMAP entry, the 17TRACK login and the Ship24 and Ollama keys are stored in the config entry (`.storage`, like any
other integration); everything else is an option.

**Options.** Settings > Devices & services > Parcels > Configure repeats the same pages with the current values.
Password fields stay blank; leave them blank to keep the stored value. The integration reloads when you save.
**Reconfigure** (three-dot menu) picks a different IMAP entry. To change the login or the folder, edit the IMAP entry.

**Address check.** An email that names a shipping address is compared with your postcode (decisive when both have one)
or, failing that, your home names (for example the name of your Amazon address-book entry). A parcel for another
address is marked "elsewhere" and not counted as arriving today. Leave both empty to switch the check off.

**Orders without a tracking number.** Some Shopify shops never email the number. When switched on, Parcels opens the
order link from the shop's email a set number of days after the order. A number shown there is added. A page that shows
none yet is checked again every few days; a page that needs a login or a browser, or still shows nothing at the end, gets
the parcel flagged ("No tracking number: look it up in the shop") with the order and Shop links on its tile, so you can
add the number yourself: `text.parcels_slot_N_tracking`, `parcels.add_tracking`, or the to-do item's description.
Expect the flag often: Shopify's newer order pages are drawn by a script and hide the number without a login, and Parcels
does not work around that. Shopify also limits visits without a login, so each check may use one up.

### Gmail and Outlook

Set these up in the IMAP integration, not in Parcels.

- **Gmail:** server `imap.gmail.com`, port 993. Turn on 2-Step Verification, then create an app password (Google
  account > Security > App passwords) and use it as the IMAP password. IMAP must be enabled in Gmail settings. The folder
  is `INBOX`, or a label name.
- **Outlook / Microsoft 365:** server `outlook.office365.com`, port 993, with an app password if your account requires
  one. Some organisations disable password IMAP; use a personal mailbox then.

### A separate inbox just for deliveries (recommended)

You don't have to give Home Assistant your main mailbox. Create a second mailbox that only receives
shipping emails, and let your main mailbox forward them there. Home Assistant (and the AI, if you use
one) then only ever sees what you chose to forward, and the password it holds opens nothing else.

**1. Create the delivery inbox (Gmail example)**

1. Create a new Google account, e.g. `yourname.parcels@gmail.com`.
2. In that account turn on 2-Step Verification (myaccount.google.com → Security), then create an
   app password at myaccount.google.com/apppasswords. Copy the 16 characters.
3. In Home Assistant add the **IMAP** integration with server `imap.gmail.com`, port `993`, the new
   address as username, the app password, folder `INBOX`. Its sensor should show a number: mail access works.

**2. Forward shipping emails from your main Gmail**

1. In your main Gmail: Settings (gear) → See all settings → **Forwarding and POP/IMAP** → Add a
   forwarding address → enter the delivery address. Google sends a confirmation code to the delivery
   inbox: open it there (Gmail web, signed in as the new account) and confirm.
   Leave "Disable forwarding" selected on that page: you only want *some* mail forwarded, by filter.
2. Create a filter: click the search options icon in the Gmail search bar and fill in
   - **From**: the shops and carriers you use, e.g.
     `amazon.co.uk OR royalmail.com OR evri.com OR dpd.co.uk OR ups.com OR dhl.com OR inpost.co.uk OR ebay.co.uk`
   - or **Has the words**, to catch shops you haven't listed:
     `subject:(dispatched OR shipped OR "on its way" OR "out for delivery" OR delivered OR tracking)`
   Click **Create filter**, tick **Forward it to** → the delivery address, then **Create filter**.
   You can make several filters (one by sender, one by subject).
3. In the delivery inbox, make one filter with **Has the words** `*` → **Never send it to Spam**, so
   forwarded shop emails are not lost to its spam folder.

Forwarding by filter keeps the original sender, so Parcels still recognises the shop and the carrier.
Filters only act on new mail; to add an older email, forward it by hand ("Forward" in Gmail): Parcels
reads the "Forwarded message" header to find the original sender and date.

**Outlook.com / Hotmail**: Settings → Mail → Rules → Add new rule → condition "From" (or
"Subject includes") → action **Forward to** the delivery address. Work or school accounts often block
forwarding to outside addresses; ask your administrator.

## Entities

One service device, "Parcels":

| Entity | Description |
|---|---|
| `todo.parcels` | Parcels in flight plus those received in the last 7 days. Ticking an item = received; add one by hand (put `tracking: <number>` in the description to track it). |
| `sensor.parcels_in_flight` | Number of parcels in flight. Attributes: `arriving_today`, `out_for_delivery`, `delivered_to_confirm`, `follow_up`, `details_pending`, `more`, `delivered_log`, `last_scan`, `last_error`, `last_mile`. |
| `sensor.parcels_slot_1` ... `_8` | One row each, ordered delivered-to-confirm, flagged, then by ETA. State is the title ("Amazon: USB-C hub") or `none`. Attributes: `parcel_id`, `title`, `eta`, `status`, `carrier`, `tracking`, `tracking_numbers`, `live`, `last_event`, `location`, `url`, `carrier_url`, `order_url`, `shop_url`, `t17_url`, `needs_tracking`, `order`, `merchant`, `emails`, `flagged`, `flag_reason`, `delivered`, `received`, `details_pending`, `possible_duplicate_of`, `ship_to`, `elsewhere`, `line`. |
| `text.parcels_slot_N_tracking` | Type the tracking number of the parcel in slot N. Available only while it has none. |
| `binary_sensor.parcels_ai_online` | The configured AI answers (AI Task entity available, or Ollama reachable with the model). Always off in rules-only mode. Attribute `pending_emails`. |
| `button.parcels_slot_N_received` / `_follow_up` | Act on whichever parcel is currently in slot N (available while the slot is filled). |

Entity IDs follow the names above for a default setup.

## Services

| Service | Fields | Description |
|---|---|---|
| `parcels.received` | `parcel_id` or `slot`, `received` (default true) | Confirm reception (false = undo). Archives the parcel in 17TRACK. |
| `parcels.follow_up` | `parcel_id` or `slot`, `flag`, `reason` | Flag or unflag; omit `flag` to toggle. |
| `parcels.merge` | `parcel_id`, `into` | Merge a duplicate into another parcel. |
| `parcels.remove` | `parcel_id` or `slot` | Delete a parcel (wrong match, spam). |
| `parcels.add_tracking` | `parcel_id` or `slot`, `tracking` | Add a number to a parcel that has none. |
| `parcels.scan` | - | Check the mailbox, retry pending emails and refresh 17TRACK now. |
| `parcels.reload_rules` | - | Read `rules.yaml` and `parcels_rules.yaml` again (see [Adapting the rules](#adapting-the-rules-to-your-country)). A wrong file raises a repair and keeps the previous rules. |
| `parcels.test_email` | `subject`, `sender`, `body` (text or HTML), `date` | Runs the rules on a made-up email and returns what they read. Response only; nothing is stored. |
| `parcels.delivered` | `since` (default 30 days) | Returns delivered or received parcels with their order lines (merchant, order number, items). Response only. |

## Events

Parcels fires `parcels_update` on the event bus with `kind`, `parcel_id`, `title`, `line`, `status`, `flag_reason`,
`url` and `elsewhere`. Kinds: `new`, `out_for_delivery`, `pickup`, `delivered`, `problem`, `follow_up`.

```yaml
automation:
  - alias: Parcel notifications
    triggers:
      - trigger: event
        event_type: parcels_update
        event_data:
          kind: out_for_delivery
      - trigger: event
        event_type: parcels_update
        event_data:
          kind: delivered
      - trigger: event
        event_type: parcels_update
        event_data:
          kind: problem
    conditions:
      - condition: template
        value_template: "{{ not trigger.event.data.elsewhere }}"
    actions:
      - action: notify.notify          # your notify service
        data:
          title: "Parcel: {{ trigger.event.data.kind | replace('_', ' ') }}"
          message: "{{ trigger.event.data.title }}. {{ trigger.event.data.line }}"
          data:
            url: "{{ trigger.event.data.url or '/' }}"
```

## Example dashboard

[`examples/dashboard.yaml`](examples/dashboard.yaml) uses only core cards: a summary, one section per slot (status,
ETA, last carrier event, link, Received and Follow up buttons), a detail page per parcel and the to-do list. Create a
dashboard with the URL `dashboard-parcels` (or edit the navigation paths) and paste it into the raw configuration
editor.

A parcel without a tracking number also shows its order and Shop links and a box to type the number, in the list and on
its detail page. Both disappear once it has one. The parts below can be added to a dashboard of your own; repeat them
per slot (1 to 8).

A parcel, shown only while the slot is filled:

```yaml
type: markdown
content: >-
  {% set a = (states.sensor.parcels_slot_1.attributes if states.sensor.parcels_slot_1 else {}) %}
  **{{ states('sensor.parcels_slot_1') }}**<br>
  {{ a.get('status') }} · {{ a.get('eta') or 'no date yet' }}
  {% if a.get('flagged') %}<br>Follow up: {{ a.get('flag_reason') }}{% endif %}
  {% if a.get('url') %}<br>[{{ a.get('tracking') or 'Track' }}]({{ a.get('url') }}){% endif %}
  {% if a.get('needs_tracking') and a.get('shop_url') %}<br>[Open in Shop]({{ a.get('shop_url') }}){% endif %}
  {% if a.get('needs_tracking') and a.get('order_url') %}<br>[View order]({{ a.get('order_url') }}){% endif %}
visibility:
  - condition: state
    entity: sensor.parcels_slot_1
    state_not: none
```

The number box, shown only while the parcel has no number:

```yaml
type: entities
entities:
  - entity: text.parcels_slot_1_tracking
    name: Add tracking number
visibility:
  - condition: state
    entity: text.parcels_slot_1_tracking
    state_not: unavailable
```

A notification when you need to look a number up:

```yaml
alias: Parcel needs a tracking number
triggers:
  - trigger: event
    event_type: parcels_update
    event_data:
      kind: follow_up
conditions:
  - condition: template
    value_template: "{{ (trigger.event.data.flag_reason or '').startswith('No tracking number') }}"
actions:
  - action: notify.notify          # your notify service
    data:
      title: Parcel without a tracking number
      message: "{{ trigger.event.data.title }}: look the number up in the shop, then add it."
      data:
        url: /dashboard-parcels/parcels          # opened by tapping the notification (iOS; clickAction on Android)
```

## Privacy

- Email bodies are processed in memory and not stored. Stored per email: IMAP UID, Message-ID, date, subject (120
  characters) and a short note.
- With an AI Task entity, the text of shipping-related emails (not other mail) is sent to whatever service is behind
  that entity. With Ollama it is sent to your Ollama server (nothing leaves your network if it is local).
- 17TRACK and Ship24 receive tracking numbers (and a short parcel title for 17TRACK), not email content.
- The order page check (off by default) visits the shop's site from your Home Assistant. The order link signs in to that
  order: it is kept in Home Assistant's storage (so in backups) and shown as an attribute, left out of the recorder history
  and never logged. The shop sees the visit; only the number is kept.
- Use a dedicated mailbox that receives only shipping mail (a forwarding filter in your main account works) with its own
  app password, so Parcels never has access to your main mailbox.
- Credentials live in Home Assistant's config entry storage; keep your Home Assistant backups safe.
- The slot sensors' `emails` attribute (subjects and notes) is excluded from the recorder history.

## Troubleshooting

- **"That IMAP entry is not loaded"** (setup), or Parcels waits and retries ("not ready"). Open Settings > Devices &
  services > IMAP and fix that entry first. Wrong login (Gmail and Outlook need an app password) and wrong folder
  (names are case-sensitive; Gmail labels appear as folders of the same name, nested ones as `Parent/Child`) are
  reported there. Parcels starts as soon as the IMAP entry loads.
- **"Ship24 refused this API key" / "Ship24 did not answer".** The key is tested when you save it. Check it at
  ship24.com, or try again later if Ship24 was unreachable.
- **Nothing appears.** Check `sensor.parcels_in_flight` attributes `last_scan` and `last_error`, then call
  `parcels.scan`. Only mail from the last 30 days is read on the first run, and only shipping-related emails create
  parcels. Enable debug logging with `logger: logs: custom_components.parcels: debug`.
- **"details pending" never clears / AI offline.** `binary_sensor.parcels_ai_online` is off: for AI Task the entity is
  unavailable; for Ollama the server is unreachable or the model is not pulled (`ollama pull <model>`). Pending emails
  are retried every minute while the AI is down, then processed when it returns. A reply that is not usable JSON is
  retried three times, then the rule result is kept. With AI set to none nothing is ever pending.
- **17TRACK not updating.** The login is checked once at setup; if the password changed, update it under Configure. The
  Home Assistant log shows one warning when the login fails.
- **Parcel for someone else.** Set the home postcode and names so such parcels are marked "elsewhere".

## Limitations

- The built-in carrier and sender patterns are UK-centric (Royal Mail, Evri, DPD, Yodel, InPost, DHL eCommerce UK, Amazon
  Logistics) and the built-in phrases are English; other carriers are found by AI or generic patterns but less reliably.
  For another country or language, add your own rules: see [Adapting the rules to your country](#adapting-the-rules-to-your-country).
- Amazon Logistics (TBA...) numbers cannot be tracked outside Amazon, so those parcels rely on Amazon's emails.
- Mail access is whatever Home Assistant's IMAP integration supports (no OAuth; app passwords). The inbox is polled every 5 minutes (no push).
- Not tested here against every provider; the rule parser is tuned on a small set of real emails.

## Development

```
uv venv && uv pip install -r requirements_test.txt
.venv/bin/python -m pytest -q
```

The tests cover the pure modules (parser, model, mailbox parsing, AI answer validation, config-flow data handling)
without Home Assistant. `tests_ha/` runs the setup and options flows and the entry setup inside a real Home Assistant
(IMAP connection mocked, no network):

```
uv venv .venv-ha && uv pip install --python .venv-ha/bin/python -r requirements_test_ha.txt
.venv-ha/bin/python -m pytest -q -o asyncio_mode=auto tests_ha
```

Licence: MIT.

## Publishing a new version

The Marketplace takes the version from the latest GitHub **release** (a tag alone is not enough). To publish:
bump `version` in `custom_components/parcels/manifest.json`, commit, then create a GitHub release named and tagged
like `v1.1.0`. The `Validate` workflow runs the Marketplace (HACS) checks, hassfest and the unit tests on every push;
keep it green. Getting into the Marketplace catalog (so people find Parcels without the link) means a pull request
to `hacs/default` once the checks pass and a release exists; reviews take months.

## Legacy mode

Installs that were created before the config flow existed (a Parcels entry with empty data) keep working: they borrow
the Home Assistant `imap`, `ollama` and `seventeentrack` integrations' config entries and the `imap_content` event,
exactly as before. New entries pick an IMAP entry and keep their own 17TRACK, Ship24 and AI settings.
