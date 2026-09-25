# Ladeplan – opsætning (ca. 20 minutter)

Alt her er gratis. Du skal bruge din GitHub-konto, Zaptec-login og Bluelink-login.
Logins indtaster du kun hos GitHub som krypterede "secrets" – de ligger ikke i nogen fil.

## 1. Opret repo'et

1. Log ind på github.com → tryk **+** øverst til højre → **New repository**.
2. Navn: `ladeplan`. Vælg **Public** (giver ubegrænsede gratis kørsler – der ligger ingen personlige oplysninger i filerne, kun batteri-% og ladeplan).
   Vil du hellere have **Private**, så ret `*/15` til `*/30` i `.github/workflows/charge.yml` for at holde dig under de 2.000 gratis minutter om måneden.
3. Tryk **Create repository**.
4. Tryk **uploading an existing file** og træk alle filerne fra zip'en ind (også mappen `.github`).
   På telefon: brug github.com i browseren, eller upload fra en computer – det er nemmest.
5. Tryk **Commit changes**.

## 2. Slå GitHub Pages til (selve appen)

Settings → **Pages** → Source: *Deploy from a branch* → Branch: **main** / **(root)** → Save.
Efter et minut ligger appen på `https://DITBRUGERNAVN.github.io/ladeplan/`.
Åbn den på telefonen og vælg **Føj til hjemmeskærm** – så virker den som en app.

## 3. Indtast dine logins (secrets)

Settings → **Secrets and variables** → **Actions** → **New repository secret**. Opret disse:

| Navn | Værdi |
|---|---|
| `ZAPTEC_USER` | din e-mail til Zaptec-appen |
| `ZAPTEC_PASSWORD` | dit Zaptec-kodeord |
| `BLUELINK_USER` | din e-mail til Bluelink-appen |
| `BLUELINK_PASSWORD` | dit Bluelink-kodeord |
| `BLUELINK_PIN` | din 4-cifrede Bluelink-PIN |

`ZAPTEC_CHARGER_ID` er kun nødvendig, hvis du har flere ladere på kontoen.

## 4. Slå kørslerne til

Fanen **Actions** → tryk **I understand my workflows, go ahead and enable them** (hvis den spørger)
→ vælg **Ladeplan** i venstre side → **Run workflow** → **Run workflow**.
Efter ca. et minut skal kørslen være grøn. Tryk på den og på **Kør ladeplan** for at se, hvad den gjorde
(batteri-%, plan, om Zaptec blev startet/stoppet).

Herefter kører den selv hvert kvarter.

## 5. Forbind appen til GitHub (så knapperne styrer laderen)

Appen skal kunne gemme din plan i repo'et. Det kræver et token:

1. github.com → dit profilbillede → **Settings** → nederst **Developer settings**
   → **Personal access tokens** → **Fine-grained tokens** → **Generate new token**.
2. Navn: `ladeplan-app`. Expiration: vælg gerne 1 år.
3. Repository access: **Only select repositories** → vælg `ladeplan`.
4. Permissions → Repository permissions: **Contents: Read and write** og **Actions: Read and write**.
5. **Generate token** og kopiér det (det vises kun én gang).
6. Åbn appen → ⚙ → udfyld **GitHub-repo** (`DITBRUGERNAVN/ladeplan`) og **GitHub-token** → Gem.

Nu sender appen planen til GitHub, hver gang du ændrer noget, og starter en kørsel med det samme,
så "Start ladning nu" og "Planlæg billigst" slår igennem på laderen inden for 1–2 minutter.

## Sådan hænger det sammen

```
Telefon (appen)  --plan.json-->  GitHub  --hvert kvarter-->  charge.py
                 <--state.json--                              │
                                          Energi Data Service ┤ priser + TREFOR-tarif
                                          Hyundai Bluelink    ┤ batteri-%
                                          Zaptec API          ┘ start / stop
```

## Godt at vide

- **Bluelink** er et uofficielt API. Hyundai begrænser antal opslag, så scriptet spørger bilen direkte
  højst én gang i timen (oftere mens der lades) og bruger ellers Hyundais gemte tal. Fejler Bluelink,
  bruger scriptet den batteri-%, du selv har tastet i appen.
- **Zaptec**: Ladningen skal være godkendt i Zaptec-appen (fri ladning eller din bruger), ellers kan
  scriptet kun pause/genoptage en allerede godkendt session.
- **Timing**: GitHub kører "hvert kvarter" med nogle minutters forsinkelse. Planen regner i hele timer,
  så det betyder ikke noget i praksis.
- **Elafgift** står til 0,73 kr./kWh som skøn – ret den i appen under ⚙, når din første regning kommer.
- Noget virker ikke? Kig under **Actions** → seneste kørsel → **Kør ladeplan**. Fejlene står på dansk.
