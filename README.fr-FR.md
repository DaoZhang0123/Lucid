# Klawbot

> **Votre doublure conversationnelle pour Windows.**
> Dites à Klawbot ce que vous voulez faire — il regarde l'écran, manipule la souris, puis vous rend le bureau dans l'état attendu.
> **Sans MCP. Sans API par application. Sans plugin de navigateur.** Juste la **vision multimodale de Claude** qui pilote votre vrai clavier et votre vraie souris.

**Langues :** [English](README.md) · [简体中文](README.zh-CN.md) · **Français**

```
Vous :   « Ouvre Microsoft Teams et envoie-moi 'Hello' à moi-même »
          ↓
Klawbot :  *prend une capture d'écran*
          *voit le bureau*
          → launch_app("Microsoft Teams")
          → click(conversation avec moi-même)
          → type("Hello")  → key("enter")
          → « Fait. »
```

Klawbot est livré comme une appli de bureau Windows (`ctrlapp.exe` comme moteur + GUI Tauri/WebView2). Voici ce qu'il sait déjà faire et une bonne brassée d'exemples de prompts.

---

## Pourquoi Klawbot ?

| | RPA traditionnel / bots liés à des API | **Klawbot** |
| --- | --- | --- |
| Intégration par appli | Chaque appli demande un SDK / plugin / serveur MCP | **Zéro.** Si un humain peut l'utiliser, Klawbot le peut aussi. |
| Marche avec les applis fermées (banques, ERP, jeux, WeChat…) | ❌ rarement | ✅ les pixels restent des pixels |
| Mise en place | Des heures de glue code | Installer, choisir un LLM, taper une phrase |
| Casse à chaque mise à jour d'API | En permanence | Seulement si l'UI change visuellement |
| Coût | Verrouillage fournisseur | Apportez votre propre LLM (Anthropic / Copilot / proxy) |

---

## Ce que Klawbot sait faire aujourd'hui (`v0.3.0`)

### Vous parle
- Coquille de chat conversationnelle (Tauri 2 + SvelteKit + WebView2), icône de barre des tâches, raccourci global d'arrêt d'urgence (`Ctrl+Alt+Esc`).
- UI trilingue — **English / 简体中文 / Français** (svelte-i18n), bascule dans Paramètres.
- Trois backends LLM en un clic : **Anthropic** direct · **GitHub Copilot** OAuth · **proxy compatible OpenAI** (LiteLLM, OpenClaw, …).

### Regarde votre écran, intelligemment
- **Per-Monitor V2 DPI** + coordonnées virtuelles multi-écrans.
- **Pyramide de captures à trois niveaux** que le modèle choisit lui-même : L1 plein écran, L2 fenêtre active, L3 voisinage du curseur (resserré par UIA — colle au vrai cadre de l'élément UI sous le curseur, pas une bête boîte 200×200).
- Gestion de contexte intelligente : fenêtres de rétention par niveau + recompression JPEG des anciennes captures + résumé automatique quand le contexte dépasse le budget du modèle.
- **Mode démarrage léger :** quand fournir le L1 initial n'apporte rien, Klawbot communique seulement la taille du bureau et laisse le modèle décider s'il doit regarder.

### Pilote votre vrai bureau
- Outil `computer` complet : clic / glisser / molette / raccourci / `type` compatible chinois (via presse-papiers — contourne complètement l'IME).
- Utilitaires « zero-GUI » intégrés pour ne pas avoir à *cliquer* pour des trivialités : `read_file` / `write_file` / `run_shell` (sortie capturée, console masquée, timeout 20 s).
- Lancement natif : `launch_app("VS Code")` utilise les API Windows (raccourcis Menu Démarrer + scan des manifestes UWP MSIX), épingle la fenêtre active et évite l'aller-retour « cherche l'icône ».

### Surveille pour vous quand vous êtes ailleurs
- **Notification visuelle de la barre des tâches** — diff dHash périodique sur la barre ; si un changement candidat apparaît, un appel LLM bon marché confirme s'il s'agit d'un nouveau message et *quelle* appli l'a déclenché. **Liste blanche d'applis** par planification — Klawbot ne touche que ce que vous autorisez.
- **Auto-réponse** avec une **AUTO-REPLY SAFETY POLICY** codée en dur dans le system prompt : ne jamais divulguer d'infos perso ou de codes, ne jamais cliquer sur payer / accepter / installer, ne jamais accepter fichiers / demandes d'amis / partage d'écran, escalade-et-arrêt en cas d'ambiguïté.

### Planifications et modèles
- **Planifications** — modes cron + ponctuel + visual_notify. Pause / reprise / « exécuter maintenant ».
- **Modèles** — sauvegardez les instructions courantes, lancez en un clic.
- **Persistance de contexte par thread** — chaque réponse dans le même thread réutilise les messages précédents (après compression d'images).

### Apprend dans le temps
- **`memory.md`** — mémoire long terme injectée dans le system prompt ; Klawbot peut appeler `remember(text)` pour écrire, vous éditez sur la page Mémoire.
- **`tools.md`** — bibliothèque de « trucs et astuces » qui évolue ; Klawbot appelle `learn_tip(text)` après une exécution réussie ou ratée.
- **Fichier par appli** (`apps/<slug>.py`) — déposer un fichier = apprendre une nouvelle appli à Klawbot, avec lanceur custom + tips.
- **Apprentissage en veille** — quand vous êtes inactif depuis 5 minutes, Klawbot réfléchit en silence sur les threads terminés, en extrait des astuces et des *icon proposals* (petites icônes recadrées qu'il a repérées ; vous les acceptez sur la page Doze, il apprend ainsi le mapping icône → appli).
- **Auto-diagnostic** — moniteurs / DPI / alias Win+R / décalage des coordonnées de clic.

### Honnête sur lui-même
- Logs par exécution dans `%LOCALAPPDATA%\dev.ctrlapp\logs\threads\<thread>\` — `events.jsonl`, `messages.json`, toutes les captures, dump complet du contexte LLM.
- Trois niveaux d'autonomie : `full` / `confirm_critical` / `confirm_each`. Liste de mots-clés HITL (`delete`, `format`, `transfer`, `confirm payment`, …) qui intercepte les actions dangereuses même en `full`.

---

## Exemple de prompt — à essayer en premier

La phrase canonique à coller direct dans le chat :

> *« Ouvre Microsoft Teams et envoie-moi 'Hello' à moi-même. »*

Klawbot va :

1. `launch_app("Microsoft Teams")` (scan menu Démarrer / UWP, Teams n'a pas besoin d'être déjà ouvert).
2. Prendre une capture, repérer la liste des conversations, cliquer la discussion **avec vous-même** (celle étiquetée avec votre propre nom).
3. `type("Hello")` via le presse-papiers (les caprices d'IME ne posent pas problème), puis `key("enter")`.
4. Répondre `"Fait."` dans le chat.

Variantes à coller telles quelles :

> *« Ouvre Microsoft Teams, dans la conversation avec moi-même envoie 'Hello' cinq fois, une par ligne. »*

> *« Tous les jours ouvrés à 9 h — ouvre Microsoft Teams et envoie-moi 'Hello' à moi-même. »* (à coller dans une **planification → action : task**, cron quotidien.)

> *« (visual_notify) Quand Microsoft Teams affiche un nouveau message, ouvre la conversation avec moi-même et réponds 'Hello'. »* (à coller dans une **planification → action : visual_notify**, liste blanche `Microsoft Teams` uniquement.)

L'`AUTO-REPLY SAFETY POLICY` (côté system prompt) reste active dans la variante visual_notify : pas de fuite d'infos perso, pas d'acceptation de fichier / lien / code, escalade-et-arrêt sur tout ce qui est bizarre.

---

## Architecture, en bref

```
┌──────────── Tauri WebView (SvelteKit) ─────────────┐
│  Chat │ Planifs │ Modèles │ Mémoire │ Doze │ ⚙     │
└──────────────────────┬─────────────────────────────┘
                       │ Tauri IPC
┌──────────────────────┴─────────────────────────────┐
│   Coquille Rust — cycle de vie sidecar, params,    │
│   tray                                              │
└──────────────────────┬─────────────────────────────┘
                       │ JSON-RPC sur stdio
┌──────────────────────┴─────────────────────────────┐
│  Sidecar Python (ctrlapp.exe)                       │
│  ReAct · planifs · monitor barre · doze · mémoire   │
│        ↓ captures mss          ↓ saisie pyautogui   │
│        ↓ HTTP                                       │
│   Anthropic API   ·   GitHub Copilot   ·   proxy    │
└─────────────────────────────────────────────────────┘
```

Données utilisateur : `%LOCALAPPDATA%\dev.ctrlapp\` (config, logs, planifs, mémoire, cache d'icônes, jeton Copilot).

---

## Installation (utilisateurs finaux)

Téléchargez `ctrlapp_<version>_x64-setup.exe` depuis une release, lancez l'installateur, démarrez **Klawbot** depuis le menu Démarrer.

Au premier lancement, ouvrez **Paramètres** et choisissez un backend LLM :

- **GitHub Copilot** — cliquez sur *Sign in to GitHub Copilot* et suivez le flux device-code. Gratuit tant que vous avez un abonnement Copilot.
- **Anthropic** — collez une clé `sk-ant-…`.
- **Proxy** — pointez vers n'importe quel endpoint compatible OpenAI (par ex. [litellm-ghc-proxy-lite](https://github.com/codetrek/litellm-ghc-proxy)).

---

## Compiler depuis les sources

### Prérequis
- Windows 10 / 11
- Python 3.11+ (testé sur 3.14)
- Node.js 20+ et npm
- Toolchain Rust (stable) + **WebView2 Runtime** (préinstallé sur Win11)

### 1) Sidecar Python

```powershell
cd D:\Project\ctrlAppWithoutMCP
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e .

pip install pyinstaller
pyinstaller packaging\ctrlapp.spec
# → dist\ctrlapp.exe
```

### 2) Application Tauri

```powershell
cd app
npm install
npm run tauri build
# → app\src-tauri\target\release\bundle\nsis\ctrlapp_<ver>_x64-setup.exe
```

La coquille Rust attend `ctrlapp.exe` à côté d'elle (ou installé sous `%LOCALAPPDATA%\ctrlapp\`) ; copiez la sortie PyInstaller avant de lancer la build de dev.

---

## Utilisation en CLI (sans GUI)

La CLI originale fonctionne toujours et reste le moyen le plus rapide de tester :

```powershell
# Test de connectivité (un seul tour, ne touche pas la souris/clavier)
python -m ctrlapp --smoke-test "Qui es-tu ? Une phrase."

# Mode prudent : confirmation y/n à chaque étape
python -m ctrlapp --max-steps 4 --autonomy confirm_each `
    "Prends une capture plein écran et dis-moi combien de fenêtres sont visibles."

# Changer de modèle
python -m ctrlapp --model claude-sonnet-4.5 "Ouvre Notepad et tape hello"

# Autonomie totale (uniquement en VM / bureau jetable)
python -m ctrlapp --autonomy full "Ouvre Notepad, tape hello world, enregistre sur le Bureau"
```

`Ctrl+C` pour interrompre. Lancer la souris dans le **coin haut-gauche** déclenche le fail-safe de PyAutoGUI.

---

## Configuration

Modèle par défaut : [config.toml](config.toml). La **vraie** config utilisateur est à `%LOCALAPPDATA%\dev.ctrlapp\config.toml` — c'est celle-là qu'il faut éditer (le fichier livré est écrasé à la mise à jour).

Sections clés :

| Section | Ce qu'elle contrôle |
| --- | --- |
| `[llm]` | provider, étapes max, max_tokens, prompt-cache, temperature/top-p, rétention des captures |
| `[llm.anthropic]` / `[llm.copilot]` / `[llm.proxy]` | model + endpoint + clé par provider |
| `[logging]` | dossier de log par exécution, niveaux texte/image (`DEBUG/INFO/WARNING/ERROR/OFF`), `png/jpg`, rotation |
| `[screenshot]` | intervalles des trois niveaux, redimensionnement, rétention par niveau, seuil de détection de changement |
| `[safety]` | mots-clés HITL, raccourci d'arrêt d'urgence (`ctrl+alt+esc`), autonomie par défaut, vérif de clic, garde dialogues |
| `[input]` | `chinese_input = "clipboard"` (recommandé) ou `unicode_sendinput`, délai entre actions |
| `[visual_notify]` | fréquence de polling, seuil dHash, cooldown LLM, instruction auto-chat |
| `[doze]` | limites de la réflexion en veille |
| `[memory]` / `[tools]` | mémoire long terme + astuces : on/off et limites |
| `[fileio]` / `[shell]` | activation / sandbox des `read_file` / `write_file` / `run_shell` |

Sauvegarder dans Paramètres recharge le sidecar à chaud.

---

## Problèmes courants

- **`HTTP 500 … Connection error`** — soubresaut côté Copilot upstream ; le client retry les 5xx. Relancez.
- **`HTTP 413 Request Entity Too Large`** — trop de captures accumulées. Réduisez `[llm].keep_recent_screenshots`, `[screenshot].l1_max_long_edge`, `--max-steps`, ou passez `[logging].image_format` à `"jpg"`.
- **`AuthenticationError: Failed to refresh API key`** — le token device-login Copilot a expiré. Reconnectez-vous depuis Paramètres.
- **`No such model …`** — modèle non activé sur votre proxy ou ID erroné. Changez dans Paramètres.
- **`BitBlt: Access Denied`** — Windows est sur l'écran de verrouillage / le bureau sécurisé Winlogon. Déverrouillez ; ou utilisez *display off* (`nircmd monitor off`) au lieu de *lock* pour que les captures continuent à fonctionner.
- **Saisie chinoise corrompue** — assurez-vous que `[input].chinese_input = "clipboard"` (défaut). Cela contourne complètement l'IME.
- **Clics décalés en multi-écrans** — gardez la même mise à l'échelle sur tous les écrans, ou ajustez `[screenshot].l1_max_long_edge` pour ne pas trop réduire l'UI.

---

## Avertissement

- Le modèle **prend complètement le contrôle de votre souris et de votre clavier**. À utiliser sur un bureau que vous pouvez interrompre, ou en VM.
- Les captures sont envoyées au backend LLM choisi (Anthropic / GitHub Copilot upstream / votre proxy).
  **Fermez ou minimisez les fenêtres sensibles (mots de passe, banque, messages privés) avant de lancer une tâche.**
- L'auto-réponse de la barre des tâches embarque une politique de sécurité codée en dur côté system prompt (pas de divulgation de codes / adresses, pas de clic sur payer / accepter, escalade-et-arrêt en cas de doute), mais vérifiez quand même quelles applis vous mettez en liste blanche.

---

## Stargazers · benchmark vs OpenAdapt

[![GitHub stars](https://img.shields.io/github/stars/codetrek/ctrlAppWithoutMCP?style=social)](https://github.com/codetrek/ctrlAppWithoutMCP/stargazers)

On suit notre courbe par rapport au voisin spirituel [OpenAdaptAI/OpenAdapt](https://github.com/OpenAdaptAI/OpenAdapt) — même créneau (RPA générative / agent computer-use), projet plus ancien. Mise à jour mensuelle :

| Date | Klawbot ★ | OpenAdapt ★ | Note |
| --- | ---: | ---: | --- |
| 2026-05-01 | _tbd_ | ~1566 | OpenAdapt à 233 forks |
| 2026-06-01 |  |  |  |

Script de rafraîchissement :

```powershell
gh api repos/OpenAdaptAI/OpenAdapt --jq '.stargazers_count'
gh api repos/codetrek/ctrlAppWithoutMCP --jq '.stargazers_count'
```
