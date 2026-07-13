/* Hark site — progressive enhancement only. No heavy libs, no webfonts. */

(function () {
  const nav = document.querySelector("[data-nav]");
  if (!nav) return;
  const onScroll = () => {
    nav.classList.toggle("is-scrolled", window.scrollY > 12);
  };
  onScroll();
  window.addEventListener("scroll", onScroll, { passive: true });
})();

/**
 * Install picker — segmented control (bash | npm | pnpm | bun).
 * Configurable via data attributes on [data-install-picker]:
 *   data-repo-owner, data-repo-name, data-repo-ref, data-npm-package, data-skills-repo
 */
(function () {
  const root = document.querySelector("[data-install-picker]");
  if (!root) return;

  const owner = root.dataset.repoOwner || "clankercode";
  const name = root.dataset.repoName || "hark";
  const ref = root.dataset.repoRef || "master";
  const npmPackage = root.dataset.npmPackage || "@ultradyn/hark";
  const skillsRepo = root.dataset.skillsRepo || `${owner}/${name}`;
  const rawBase = `https://raw.githubusercontent.com/${owner}/${name}/${ref}`;

  /** @type {Record<string, { cmd: string, hint: string, title: string }>} */
  const commands = {
    bash: {
      cmd: `curl -fsSL ${rawBase}/install.sh | bash`,
      hint: "# installs CLI + skills to ~/.claude/skills",
      title: "bash · install.sh",
    },
    npm: {
      cmd: `npm i -g ${npmPackage}`,
      hint: `# skills: npx skills add ${skillsRepo} -g -y  ·  or: hark-skill path`,
      title: "npm · global",
    },
    pnpm: {
      cmd: `pnpm add -g ${npmPackage}`,
      hint: `# skills: npx skills add ${skillsRepo} -g -y  ·  or: hark-skill path`,
      title: "pnpm · global",
    },
    bun: {
      cmd: `bun add -g ${npmPackage}`,
      hint: `# skills: npx skills add ${skillsRepo} -g -y  ·  or: hark-skill path`,
      title: "bun · global",
    },
  };

  const cmdEl = root.querySelector("[data-install-cmd]");
  const hintEl = root.querySelector("[data-install-hint]");
  const titleEl = root.querySelector("[data-install-title]");
  const copyBtn = root.querySelector("[data-install-copy]");
  const radios = root.querySelectorAll('input[type="radio"][name="install-method"]');

  let currentCmd = commands.bash.cmd;

  function select(method) {
    const entry = commands[method] || commands.bash;
    currentCmd = entry.cmd;
    if (cmdEl) cmdEl.textContent = entry.cmd;
    if (hintEl) hintEl.textContent = entry.hint;
    if (titleEl) titleEl.textContent = entry.title;
    if (copyBtn) {
      copyBtn.textContent = "Copy";
      copyBtn.classList.remove("is-copied");
      copyBtn.setAttribute("aria-label", "Copy install command");
    }
  }

  radios.forEach((radio) => {
    radio.addEventListener("change", () => {
      if (radio.checked) select(radio.value);
    });
  });

  // Sync UI if a non-default radio is already checked (e.g. restored form state)
  const checked = root.querySelector('input[type="radio"][name="install-method"]:checked');
  if (checked && checked.value !== "bash") {
    select(checked.value);
  }

  if (copyBtn) {
    copyBtn.addEventListener("click", async () => {
      const text = currentCmd;
      try {
        if (navigator.clipboard && navigator.clipboard.writeText) {
          await navigator.clipboard.writeText(text);
        } else {
          const ta = document.createElement("textarea");
          ta.value = text;
          ta.setAttribute("readonly", "");
          ta.style.position = "fixed";
          ta.style.opacity = "0";
          document.body.appendChild(ta);
          ta.select();
          document.execCommand("copy");
          document.body.removeChild(ta);
        }
        copyBtn.textContent = "Copied";
        copyBtn.classList.add("is-copied");
        copyBtn.setAttribute("aria-label", "Install command copied");
        window.setTimeout(() => {
          copyBtn.textContent = "Copy";
          copyBtn.classList.remove("is-copied");
          copyBtn.setAttribute("aria-label", "Copy install command");
        }, 1600);
      } catch {
        copyBtn.textContent = "Failed";
        window.setTimeout(() => {
          copyBtn.textContent = "Copy";
        }, 1600);
      }
    });
  }
})();
