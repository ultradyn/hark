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
 * Install picker — segmented control (skills | bash | npm | pnpm | bun).
 * Supports multiple instances (hero strip + full #install section).
 * Configurable via data attributes on [data-install-picker]:
 *   data-install-url (bash one-liner source; default https://hark.xk.io/install.sh)
 *   data-repo-owner, data-repo-name, data-npm-package, data-skills-repo
 */
(function () {
  const roots = document.querySelectorAll("[data-install-picker]");
  if (!roots.length) return;

  roots.forEach((root) => {
    const owner = root.dataset.repoOwner || "ultradyn";
    const name = root.dataset.repoName || "hark";
    const npmPackage = root.dataset.npmPackage || "@ultradyn/hark";
    const skillsRepo = root.dataset.skillsRepo || `${owner}/${name}`;
    // Hosted on the static site (copied into Pages artifact on each version tag).
    const installUrl =
      root.dataset.installUrl || "https://hark.xk.io/install.sh";

    const compact = root.classList.contains("install-picker--compact");

    /** @type {Record<string, { cmd: string, hint: string, title: string }>} */
    const commands = {
      skills: {
        cmd: `npx skills add ${skillsRepo} -g -y`,
        hint: compact
          ? "# skills only · still need hark CLI for handsfree"
          : "# agent skills only · still need hark CLI (bash install or uv) for handsfree",
        title: "skills · npx",
      },
      bash: {
        cmd: `curl -fsSL ${installUrl} | bash`,
        hint: compact
          ? "# CLI + skills · script from hark.xk.io"
          : "# installs CLI + skills to ~/.claude/skills · script from hark.xk.io",
        title: "bash · hark.xk.io",
      },
      npm: {
        cmd: `npm i -g ${npmPackage}`,
        hint: compact
          ? `# package skills · or npx skills add ${skillsRepo}`
          : `# package skills · or: npx skills add ${skillsRepo} -g -y`,
        title: "npm · global",
      },
      pnpm: {
        cmd: `pnpm add -g ${npmPackage}`,
        hint: compact
          ? `# package skills · or npx skills add ${skillsRepo}`
          : `# package skills · or: npx skills add ${skillsRepo} -g -y`,
        title: "pnpm · global",
      },
      bun: {
        cmd: `bun add -g ${npmPackage}`,
        hint: compact
          ? `# package skills · or npx skills add ${skillsRepo}`
          : `# package skills · or: npx skills add ${skillsRepo} -g -y`,
        title: "bun · global",
      },
    };

    const cmdEl = root.querySelector("[data-install-cmd]");
    const hintEl = root.querySelector("[data-install-hint]");
    const titleEl = root.querySelector("[data-install-title]");
    const copyBtn = root.querySelector("[data-install-copy]");
    // Scope radios to this picker only (hero vs #install use different name=)
    const radios = root.querySelectorAll('input[type="radio"]');

    let currentCmd = commands.skills.cmd;

    function select(method) {
      const entry = commands[method] || commands.skills;
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
    const checked = root.querySelector('input[type="radio"]:checked');
    if (checked && checked.value !== "skills") {
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
  });
})();
