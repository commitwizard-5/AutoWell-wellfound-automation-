(() => {
  const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  const nav = document.querySelector("[data-site-nav]");
  const menuToggle = document.querySelector(".menu-toggle");
  const primaryNav = document.querySelector(".primary-nav");

  const closeMenu = () => {
    if (!menuToggle || !primaryNav) return;
    menuToggle.setAttribute("aria-expanded", "false");
    menuToggle.setAttribute("aria-label", "Open navigation");
    primaryNav.classList.remove("is-open");
  };

  if (menuToggle && primaryNav) {
    menuToggle.addEventListener("click", () => {
      const isOpen = menuToggle.getAttribute("aria-expanded") === "true";
      menuToggle.setAttribute("aria-expanded", String(!isOpen));
      menuToggle.setAttribute("aria-label", isOpen ? "Open navigation" : "Close navigation");
      primaryNav.classList.toggle("is-open", !isOpen);
    });

    primaryNav.addEventListener("click", (event) => {
      if (event.target instanceof Element && event.target.closest("a")) closeMenu();
    });

    document.addEventListener("keydown", (event) => {
      if (event.key === "Escape") closeMenu();
    });
  }

  document.querySelectorAll('[role="tablist"]').forEach((tablist) => {
    const tabs = Array.from(tablist.querySelectorAll('[role="tab"]'));
    if (!tabs.length) return;
    tabs.forEach((tab) => {
      tab.tabIndex = tab.getAttribute("aria-selected") === "true" ? 0 : -1;
    });
    tablist.addEventListener("keydown", (event) => {
      if (!(event.target instanceof HTMLElement)) return;
      const currentIndex = tabs.indexOf(event.target);
      if (currentIndex < 0) return;
      let nextIndex = currentIndex;
      if (event.key === "ArrowRight" || event.key === "ArrowDown") nextIndex = (currentIndex + 1) % tabs.length;
      else if (event.key === "ArrowLeft" || event.key === "ArrowUp") nextIndex = (currentIndex - 1 + tabs.length) % tabs.length;
      else if (event.key === "Home") nextIndex = 0;
      else if (event.key === "End") nextIndex = tabs.length - 1;
      else return;
      event.preventDefault();
      tabs[nextIndex].focus();
      tabs[nextIndex].click();
    });
  });

  let scrollFrame = 0;
  const updateNav = () => {
    if (nav) nav.classList.toggle("is-scrolled", window.scrollY > 24);
    scrollFrame = 0;
  };
  window.addEventListener("scroll", () => {
    if (!scrollFrame) scrollFrame = window.requestAnimationFrame(updateNav);
  }, { passive: true });
  updateNav();

  const hero = document.querySelector(".mountain-hero");
  const heroPhoto = document.querySelector(".mountain-photo");
  if (hero && heroPhoto && !reducedMotion && window.matchMedia("(min-width: 761px)").matches) {
    let pointerFrame = 0;
    let pointerX = 0;
    let pointerY = 0;

    hero.addEventListener("pointermove", (event) => {
      pointerX = ((event.clientX / window.innerWidth) - 0.5) * -9;
      pointerY = ((event.clientY / window.innerHeight) - 0.5) * -7;
      if (!pointerFrame) {
        pointerFrame = window.requestAnimationFrame(() => {
          heroPhoto.style.setProperty("--parallax-x", `${pointerX}px`);
          heroPhoto.style.setProperty("--parallax-y", `${pointerY}px`);
          pointerFrame = 0;
        });
      }
    }, { passive: true });

    hero.addEventListener("pointerleave", () => {
      heroPhoto.style.setProperty("--parallax-x", "0px");
      heroPhoto.style.setProperty("--parallax-y", "0px");
    });
  }

  const revealItems = document.querySelectorAll(".reveal");
  if (!reducedMotion && "IntersectionObserver" in window) {
    document.documentElement.classList.add("motion-ready");
    const revealObserver = new IntersectionObserver((entries, observer) => {
      entries.forEach((entry) => {
        if (!entry.isIntersecting) return;
        entry.target.classList.add("is-visible");
        observer.unobserve(entry.target);
      });
    }, { threshold: 0.12, rootMargin: "0px 0px -24px 0px" });

    revealItems.forEach((item, index) => {
      if (item.classList.contains("capability-row")) {
        item.style.setProperty("--reveal-delay", `${(index % 4) * 55}ms`);
      }
      revealObserver.observe(item);
    });
  } else {
    revealItems.forEach((item) => item.classList.add("is-visible"));
  }

  const codeStages = [
    {
      key: "discovery",
      title: "autoapply/autoapply.py",
      lines: [
        "def scrape_jobs(self, maximum_jobs: int = 30, filters: dict[str, str] | None = None):",
        "    \"\"\"Apply filters on Wellfound and collect result cards.\"\"\"",
        "    filters = filters or {}",
        "    self.scrape_warning = \"\"",
        "    warnings: list[str] = []",
        "    self.driver.get(\"https://wellfound.com/jobs\")",
        "    self._apply_search_criteria(filters, warnings)",
        "    self._apply_wellfound_filters(filters, warnings)",
        "    self._report(\"Collecting filtered Wellfound job cards…\")",
        "    self.data = self._listing_jobs(maximum_jobs)",
      ],
      highlight: [0, 5, 6, 7, 9],
    },
    {
      key: "resume",
      title: "autoapply/helper/genai.py",
      lines: [
        "project_links = self.pdf_handler.extract_links() if self.resume_path else []",
        "resume_text = self.pdf_handler.extract_text() if self.resume_path else \"\"",
      ],
      highlight: [0, 1],
    },
    {
      key: "letter",
      title: "autoapply/helper/genai.py",
      lines: [
        "letter, provider = self._generate_from_providers(",
        "    prompt,",
        "    self.generation_config,",
        "    validator=self._valid_cover_letter,",
        "    error_details=errors_seen,",
        ")",
        "if letter is None:",
        "    letter = self._template_cover_letter(",
        "        role, company, description, resume_text, project_links",
        "    )",
        "    provider = \"template\"",
      ],
      highlight: [0, 6, 7, 10],
    },
    {
      key: "application",
      title: "autoapply/helper/gotolink.py",
      lines: [
        "if unanswered:",
        "    status = \"Needs review\"",
        "    error_message = (",
        "        \"Required application questions need a confirmed answer: \"",
        "        + \"; \".join(unanswered[:5])",
        "    )",
        "    self._report(\"[FORM] Submission paused until required answers are reviewed.\")",
        "    break",
      ],
      highlight: [0, 1, 2, 6, 7],
    },
  ];

  const escapeHtml = (value) => value.replace(/[&<>"']/g, (character) => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    "\"": "&quot;",
    "'": "&#39;",
  })[character]);

  const formatCodeLine = (line) => {
    const matcher = /(#.*$)|("(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*')|\b(def|return|if|else|for|in|None|True|False|not|or|and)\b|\b(self|str|int|list|dict|bool|range)\b|\b\d+\b/g;
    let result = "";
    let position = 0;
    let match;
    while ((match = matcher.exec(line)) !== null) {
      result += escapeHtml(line.slice(position, match.index));
      const token = escapeHtml(match[0]);
      let tokenClass = "code-name";
      if (match[1]) tokenClass = "code-comment";
      else if (match[2]) tokenClass = "code-string";
      else if (match[3]) tokenClass = "code-keyword";
      else if (match[4]) tokenClass = "code-operator";
      else if (match[5]) tokenClass = "code-number";
      result += `<span class="${tokenClass}">${token}</span>`;
      position = matcher.lastIndex;
    }
    result += escapeHtml(line.slice(position));
    result = result.replace(/(<span class="code-keyword">def<\/span>\s+)([a-zA-Z_]\w*)/g, '$1<span class="code-function">$2</span>');
    return result;
  };

  const linesContainer = document.querySelector("[data-code-lines]");
  const fileLabel = document.querySelector("[data-terminal-file]");
  const progressLabel = document.querySelector("[data-terminal-progress]");
  const chapterButtons = Array.from(document.querySelectorAll("[data-terminal-chapter] .chapter-button"));
  let currentCodeKey = "";

  const showCodeStage = (stage) => {
    if (!linesContainer || !fileLabel || !progressLabel || currentCodeKey === stage.key) return;
    currentCodeKey = stage.key;
    fileLabel.textContent = stage.title;
    const listItems = stage.lines.map((line, index) => {
      const item = document.createElement("li");
      item.dataset.line = String(index + 1).padStart(2, "0");
      item.innerHTML = formatCodeLine(line);
      if (stage.highlight.includes(index)) item.classList.add("is-highlighted");
      item.style.opacity = reducedMotion ? "1" : "0";
      return item;
    });
    linesContainer.replaceChildren(...listItems);
    progressLabel.textContent = `${stage.key.toUpperCase()} · ${String(codeStages.indexOf(stage) + 1).padStart(2, "0")} / 04`;
    if (reducedMotion) return;
    listItems.forEach((item, index) => {
      window.setTimeout(() => {
        item.style.opacity = "1";
      }, index * 36);
    });
  };

  const activateChapter = (chapter) => {
    const key = chapter.dataset.terminalChapter;
    const stage = codeStages.find((item) => item.key === key);
    if (!stage) return;
    document.querySelectorAll("[data-terminal-chapter]").forEach((item) => {
      const active = item === chapter;
      item.classList.toggle("is-active", active);
      const button = item.querySelector(".chapter-button");
      if (button) button.setAttribute("aria-pressed", String(active));
    });
    showCodeStage(stage);
  };

  if (chapterButtons.length) {
    chapterButtons.forEach((button) => {
      button.addEventListener("click", () => {
        const chapter = button.closest("[data-terminal-chapter]");
        if (chapter) {
          activateChapter(chapter);
          chapter.scrollIntoView({ behavior: reducedMotion ? "auto" : "smooth", block: "center" });
        }
      });
    });

    if ("IntersectionObserver" in window && !reducedMotion) {
      const chapterObserver = new IntersectionObserver((entries) => {
        const visible = entries
          .filter((entry) => entry.isIntersecting)
          .sort((left, right) => right.intersectionRatio - left.intersectionRatio)[0];
        if (visible) activateChapter(visible.target);
      }, { threshold: [0.2, 0.45, 0.7], rootMargin: "-13% 0px -13% 0px" });
      document.querySelectorAll("[data-terminal-chapter]").forEach((chapter) => chapterObserver.observe(chapter));
    }

    const initialChapter = document.querySelector("[data-terminal-chapter]");
    if (initialChapter) activateChapter(initialChapter);
  }

  const demoJobs = {
    engineer: {
      button: "demo-role-one",
      company: "EXAMPLE COMPANY",
      title: "Software Engineer",
      description: "A representative role for the AutoWell walkthrough. In the actual dashboard, imported Wellfound cards include their available listing details and a link to the original post.",
      skills: "Wellfound listing · role review · sample data",
      letter: "“Hello, I’m exploring the Software Engineer opportunity at Example Company. This sample illustrates where a tailored letter may be prepared using the selected job and configured candidate information.”",
    },
    product: {
      button: "demo-role-two",
      company: "SAMPLE STARTUP",
      title: "Product Engineer",
      description: "A sample hybrid product engineering opportunity. AutoWell’s real importer collects visible listing-card details and lets you inspect the original Wellfound post before choosing a role.",
      skills: "Product engineering · hybrid · sample data",
      letter: "“Hello, I’m interested in the Product Engineer opportunity at Sample Startup. This sample illustrates how a role-specific introduction can be prepared from the job description and confirmed candidate experience.”",
    },
    data: {
      button: "demo-role-three",
      company: "DEMO COMPANY",
      title: "Data Engineer",
      description: "A representative remote data engineering role. The real application flow reads the job description before preparing a letter and identifying the employer’s application fields.",
      skills: "Data engineering · remote · sample data",
      letter: "“Hello, I’m exploring the Data Engineer role at Demo Company. This illustrative text demonstrates the personalized-letter step; it is not generated by the live application or sent to a provider.”",
    },
  };

  const demoTitle = document.querySelector("[data-demo-title]");
  const demoCompany = document.querySelector("[data-demo-company]");
  const demoDescription = document.querySelector("[data-demo-description]");
  const demoSkills = document.querySelector("[data-demo-skills]");
  const demoLetter = document.querySelector("[data-demo-letter]");
  const demoPanel = document.querySelector("#demo-job-panel");

  document.querySelectorAll("[data-demo-job]").forEach((button) => {
    button.addEventListener("click", () => {
      const job = demoJobs[button.dataset.demoJob];
      if (!job || !demoTitle || !demoCompany || !demoDescription || !demoSkills || !demoLetter || !demoPanel) return;
      document.querySelectorAll("[data-demo-job]").forEach((item) => {
        const selected = item === button;
        item.classList.toggle("is-selected", selected);
        item.setAttribute("aria-selected", String(selected));
        item.tabIndex = selected ? 0 : -1;
      });
      demoPanel.setAttribute("aria-labelledby", job.button);
      demoTitle.textContent = job.title;
      demoCompany.textContent = `${job.company} · SAMPLE ROLE`;
      demoDescription.textContent = job.description;
      demoSkills.textContent = job.skills;
      demoLetter.textContent = job.letter;
    });
  });

  const stageNames = ["REVIEW", "PREPARE", "CONFIRM"];
  const stageStatus = ["Needs your review", "Ready for a candidate check", "Waiting on your decision"];
  const progress = document.querySelector("[data-demo-progress]");
  const stageLabel = document.querySelector("[data-demo-stage-label]");
  const statusLabel = document.querySelector(".demo-status");
  document.querySelectorAll("[data-demo-stage]").forEach((button) => {
    button.addEventListener("click", () => {
      const stage = Number(button.dataset.demoStage);
      if (!Number.isInteger(stage) || stage < 0 || stage > 2) return;
      document.querySelectorAll("[data-demo-stage]").forEach((item) => {
        const active = item === button;
        item.classList.toggle("is-current", active);
        item.setAttribute("aria-pressed", String(active));
      });
      if (progress) progress.style.width = `${((stage + 1) / stageNames.length) * 100}%`;
      if (stageLabel) stageLabel.textContent = `${String(stage + 1).padStart(2, "0")} / 03 · ${stageNames[stage]}`;
      if (statusLabel) statusLabel.textContent = stageStatus[stage];
    });
  });

  const architectureDetails = {
    wellfound: ["01", "Job discovery.", "The scraper visits the signed-in Wellfound jobs page, applies available search filters, and collects visible result cards.", "INPUT · WELFOUND JOBS"],
    scraper: ["02", "Local orchestration.", "FastAPI serves the dashboard and starts the Python job-import and application workflows on the local machine.", "BACKEND · FASTAPI + PYTHON"],
    firefox: ["03", "A real browser session.", "Selenium launches Firefox with the profile path configured in local settings. The existing profile is used for Wellfound access.", "BROWSER · SELENIUM + FIREFOX"],
    resume: ["04", "Resume context.", "When a resume PDF is configured, PyPDF2 extracts its text and links for cover-letter context.", "INPUT · PYPDF2"],
    mapping: ["05", "Answers with guardrails.", "Saved profile and question-library answers are matched to application fields. Required unsupported questions pause the workflow.", "SAFETY · CONFIRMED ANSWERS"],
    gemini: ["06", "Optional first provider.", "If a Gemini key is configured, the cover-letter generator tries Gemini first and validates returned content.", "EXTERNAL · CONFIGURABLE"],
    deepseek: ["07", "Optional fallback provider.", "If configured Gemini cannot complete a valid generation, a configured DeepSeek provider is tried next; the built-in template is the final fallback.", "EXTERNAL · FALLBACK"],
    tracking: ["08", "An application record.", "The workbook records each attempt’s time, job, letter, status, error, and provider on the local machine.", "OUTPUT · LOCAL XLSX"],
  };

  const archTitle = document.querySelector("[data-arch-title]");
  const archDescription = document.querySelector("[data-arch-description]");
  const archIndex = document.querySelector("[data-arch-index]");
  const archMeta = document.querySelector("[data-arch-meta]");
  const archNodes = document.querySelectorAll("[data-arch-node]");

  const updateArchitecture = (node, selected) => {
    const detail = architectureDetails[node.dataset.archNode];
    if (!detail || !archTitle || !archDescription || !archIndex || !archMeta) return;
    archIndex.textContent = detail[0];
    archTitle.textContent = detail[1];
    archDescription.textContent = detail[2];
    archMeta.textContent = detail[3];
    if (selected) {
      archNodes.forEach((item) => item.setAttribute("aria-pressed", String(item === node)));
    }
  };

  archNodes.forEach((node) => {
    node.addEventListener("pointerenter", () => updateArchitecture(node, false));
    node.addEventListener("focus", () => updateArchitecture(node, false));
    node.addEventListener("click", () => updateArchitecture(node, true));
  });

  const milestones = {
    idea: {
      date: "12 SEPTEMBER 2026",
      title: "A first commit, and a direction.",
      description: "The repository begins. The project’s history records a first Wellfound deployment on October 28; the milestones that follow track real changes in job discovery, provider reliability, and application review.",
      tags: ["PROJECT HISTORY", "INCREMENTAL BUILD"],
    },
    deploy: {
      date: "28 OCTOBER 2026",
      title: "The first deployment.",
      description: "The Git history records the first deployment, turning the initial project into a working job-search tool.",
      tags: ["FIRST DEPLOYMENT", "PROJECT MILESTONE"],
    },
    discovery: {
      date: "02 NOVEMBER 2026",
      title: "Search where the jobs are.",
      description: "The importer evolves to apply filters on Wellfound itself and collect visible result cards, avoiding a separate jobs API or opening each listing during import.",
      tags: ["WELLFOUND FILTERS", "VISIBLE JOB CARDS"],
    },
    ai: {
      date: "05 NOVEMBER 2026",
      title: "A fallback when AI is unavailable.",
      description: "DeepSeek was added as an optional fallback after Gemini, followed by the built-in cover-letter template when neither provider returns valid content.",
      tags: ["GEMINI", "DEEPSEEK FALLBACK", "LOCAL TEMPLATE"],
    },
    forms: {
      date: "06 NOVEMBER 2026",
      title: "Questions deserve a real answer.",
      description: "The application workflow gained form scanning, reusable question matching, dry-run support, and a review pause for required answers that cannot be confirmed.",
      tags: ["FORM DETECTION", "QUESTION MAPPING", "DRY RUN"],
    },
  };

  const milestoneDate = document.querySelector("[data-milestone-date]");
  const milestoneTitle = document.querySelector("[data-milestone-title]");
  const milestoneDescription = document.querySelector("[data-milestone-description]");
  const milestoneTags = document.querySelector("[data-milestone-tags]");
  const milestonePanel = document.querySelector("#milestone-detail");
  document.querySelectorAll("[data-milestone]").forEach((button) => {
    button.addEventListener("click", () => {
      const milestone = milestones[button.dataset.milestone];
      if (!milestone || !milestoneDate || !milestoneTitle || !milestoneDescription || !milestoneTags || !milestonePanel) return;
      document.querySelectorAll("[data-milestone]").forEach((item) => {
        const active = item === button;
        item.classList.toggle("is-active", active);
        item.setAttribute("aria-selected", String(active));
        item.tabIndex = active ? 0 : -1;
      });
      milestonePanel.setAttribute("aria-labelledby", button.id);
      milestoneDate.textContent = milestone.date;
      milestoneTitle.textContent = milestone.title;
      milestoneDescription.textContent = milestone.description;
      milestoneTags.replaceChildren(...milestone.tags.map((tag) => {
        const element = document.createElement("span");
        element.textContent = tag;
        return element;
      }));
    });
  });
})();
