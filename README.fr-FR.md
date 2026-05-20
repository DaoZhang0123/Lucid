# <img src="app/src-tauri/icons/128x128.png" width="32" alt="Lucid icon" /> Lucid

Un véritable assistant IA qui agit comme un humain sur ordinateur : sans MCP, contrôle direct de vos applications Windows, et auto-réponse continue quand vous êtes absent.

> **Un regard limpide pour votre bureau Windows — Agent visuel pour Windows.**
> Dites à Lucid ce que vous voulez faire. Il scrute l'écran, manipule la souris ; quand vous n'êtes pas là, il lit les messages entrants et répond poliment à votre place.
> **Sans MCP. Sans API par application. Sans plugin de navigateur.** Juste la **vision multimodale de Claude** qui pilote votre vrai clavier et votre vraie souris.
> **Contrairement aux bots officiels (WeChat, Slack, Teams) — Lucid contrôle votre vrai client**, donc il peut lire n'importe quel message, voir tout l'historique, répondre en votre nom, avec persistance d'état et sans enregistrement.

> **D'où vient le nom ?** *Lucid* — clair, perspicace, l'esprit transparent. Le réticule au centre du logo, c'est l'œil ; ce que l'œil voit, l'agent le fait. Une pyramide de captures à trois niveaux et un moniteur de la barre des tâches qui ne cligne jamais gardent la perception affûtée ; une seule boucle ReAct garde l'action honnête. **Lucid = l'œil qui voit + les mains qui agissent.**
> *Petit easter egg : gardez un œil sur le splash — un mini crabe vient se nicher au centre du réticule.*

**Langues :** [English](README.md) · [简体中文](README.zh-CN.md) · **Français**

<!-- VIDÉO DE DÉMO : déposez le fichier ici, p.ex. ![demo](docs/demo.mp4) ou une balise HTML <video> -->
*▶ Vidéo de démonstration bientôt disponible.*

```
Teams (entrant) :  « Transforme le doc sur mon bureau (proposal.docx)
                    en un beau jeu de diapositives et renvoie-le-moi. »
          ↓
Lucid :  *le moniteur de la barre des tâches voit la nouvelle notif Teams*
          *un LLM léger confirme : vrai message, app = Microsoft Teams*
          → launch_app("Microsoft Teams")  → ouvre la conversation, lit la demande
          → read_file("~/Desktop/proposal.docx")     # extrait le plan
          → pas encore de compétence PPT ? on la prend dans le dépôt skills d'Anthropic :
              run_shell("git clone https://github.com/anthropics/skills ~/.lucid/skills")
              read_file("~/.lucid/skills/pptx/SKILL.md")   # apprend à s'en servir
              learn_tip("utiliser anthropics/skills/pptx pour bâtir un deck depuis un plan docx")
          → run_shell("python ~/.lucid/skills/pptx/build.py proposal.docx proposal.pptx")
          → launch_app("Microsoft Teams")  → click(chat) → attach(proposal.pptx)
          → type(« Deck en pièce jointe — dis-moi si tu veux des retouches. ») → key("enter")
          → « Fait. Répondu dans Teams avec proposal.pptx. »
```

Lucid est livré comme une appli de bureau Windows (`lucid.exe` comme moteur + GUI Tauri/WebView2). Voici ce qu'il sait déjà faire et une bonne brassée d'exemples de prompts.

---

## Pourquoi Lucid ?

| | RPA traditionnel / bots liés à des API | **Lucid** |
| --- | --- | --- |
| Intégration par appli | Chaque appli demande un SDK / plugin / serveur MCP | **Zéro.** Si un humain peut l'utiliser, Lucid le peut aussi. |
| Marche avec les applis fermées (banques, ERP, jeux, WeChat…) | ❌ rarement | ✅ les pixels restent des pixels |
| Auto-réponse aux messages | Bots officiels seulement ; approbation requise ; pas d'état ; ne voit pas l'historique complet | ✅ **Contrôle votre vrai client.** Lit n'importe quel message, voit l'historique complet, répond en votre nom, avec persistance d'état. |
| Mise en place | Des heures de glue code | Installer, choisir un LLM, taper une phrase |
| Casse à chaque mise à jour d'API | En permanence | Seulement si l'UI change visuellement |
| Coût | Verrouillage fournisseur | Apportez votre propre LLM (Anthropic / Copilot / proxy) |

---

## Architecture, en bref

![Architecture de Lucid](docs/arch.fr-FR.png)

Données utilisateur : `~/.lucid/` (config, logs, planifs, mémoire, cache d'icônes, jeton Copilot).

---

## Installation (utilisateurs finaux)

Téléchargez `lucid_<version>_x64-setup.exe` depuis une release, lancez l'installateur, démarrez **Lucid** depuis le menu Démarrer.

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
cd D:\Project\Lucid
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e .

pip install pyinstaller
pyinstaller packaging\lucid.spec
# → dist\lucid.exe
```

### 2) Application Tauri

```powershell
cd app
npm install
npm run tauri build
# → app\src-tauri\target\release\bundle\nsis\lucid_<ver>_x64-setup.exe
```

La coquille Rust attend `lucid.exe` à côté d'elle (ou installé sous `%LOCALAPPDATA%\lucid\`) ; copiez la sortie PyInstaller avant de lancer la build de dev.

---

## Utilisation en CLI (sans GUI)

Lancez les commandes depuis la racine du dépôt (`D:\Project\Lucid`).

Si votre provider demande une clé, définissez-la d'abord :

```powershell
# provider proxy
$env:LITELLM_MASTER_KEY = "your_proxy_key"

# provider anthropic
$env:ANTHROPIC_API_KEY = "sk-ant-..."
```

Puis exécutez :

```powershell
cd D:\Project\Lucid

# Test de connectivité (un seul tour, ne touche pas la souris/clavier)
.venv\Scripts\python.exe -m lucid --smoke-test "Qui es-tu ? Une phrase."

# Lancer une tâche
.venv\Scripts\python.exe -m lucid `
    "Prends une capture plein écran et dis-moi combien de fenêtres sont visibles."

# Changer de modèle
.venv\Scripts\python.exe -m lucid --model claude-sonnet-4.5 "Ouvre Notepad et tape hello"

# À réserver à une VM / bureau jetable pour les actions destructrices
..\.venv\Scripts\python.exe -m lucid "Ouvre Notepad, tape hello world, enregistre sur le Bureau"
```

Si vous voyez `missing api_key (config .api_key or LITELLM_MASTER_KEY environment variable)`, renseignez `[llm.proxy].api_key` dans `~/.lucid/config.toml` ou exportez `LITELLM_MASTER_KEY`.

`Ctrl+C` pour interrompre. Lancer la souris dans le **coin haut-gauche** déclenche le fail-safe de PyAutoGUI.

---

## Configuration

Modèle par défaut : [config.toml](config.toml). La **vraie** config utilisateur est à `~/.lucid/config.toml` — c'est celle-là qu'il faut éditer (le fichier livré est écrasé à la mise à jour).

Sections clés :

| Section | Ce qu'elle contrôle |
| --- | --- |
| `[llm]` | provider, max_tokens, prompt-cache, temperature/top-p, rétention des captures |
| `[llm.anthropic]` / `[llm.copilot]` / `[llm.proxy]` | model + endpoint + clé par provider |
| `[logging]` | dossier de log par exécution, niveaux texte/image (`DEBUG/INFO/WARNING/ERROR/OFF`), `png/jpg`, rotation |
| `[screenshot]` | intervalles des trois niveaux, redimensionnement, rétention par niveau, seuil de détection de changement |
| `[safety]` | raccourci d'arrêt d'urgence (`ctrl+alt+esc`), vérif de clic, garde dialogues |
| `[input]` | `chinese_input = "clipboard"` (recommandé) ou `unicode_sendinput`, délai entre actions |
| `[visual_notify]` | fréquence de polling, seuil dHash, cooldown LLM, instruction auto-chat |
| `[doze]` | limites de la réflexion en veille |
| `[memory]` / `[tools]` | mémoire long terme + astuces : on/off et limites |
| `[fileio]` / `[shell]` | activation / sandbox des `read_file` / `write_file` / `run_shell` |

Sauvegarder dans Paramètres recharge le sidecar à chaud.

---

## Avertissement

- Le modèle **prend complètement le contrôle de votre souris et de votre clavier**. À utiliser sur un bureau que vous pouvez interrompre, ou en VM.
- Les captures sont envoyées au backend LLM choisi (Anthropic / GitHub Copilot upstream / votre proxy).
  **Fermez ou minimisez les fenêtres sensibles (mots de passe, banque, messages privés) avant de lancer une tâche.**
- L'auto-réponse de la barre des tâches embarque une politique de sécurité codée en dur côté system prompt (pas de divulgation de codes / adresses, pas de clic sur payer / accepter, escalade-et-arrêt en cas de doute), mais vérifiez quand même quelles applis vous mettez en liste blanche.

---

## Stargazers

[![GitHub stars](https://img.shields.io/github/stars/codetrek/Lucid?style=social)](https://github.com/codetrek/Lucid/stargazers)
