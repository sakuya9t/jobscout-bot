import { ref } from "vue";

// Mirrors window.setJobScoutTheme / toggleTheme from base.html. The pre-paint script
// in index.html sets the initial data-theme before mount; this keeps a reactive copy
// for the toggle and persists the choice. Both localStorage keys are written/read for
// back-compat with the legacy Jinja pages ("theme" canonical, "jobscout-theme" legacy).
type Theme = "light" | "dark";

function current(): Theme {
  return document.documentElement.dataset.theme === "dark" ? "dark" : "light";
}

const theme = ref<Theme>(current());

function setTheme(next: Theme): void {
  theme.value = next;
  document.documentElement.dataset.theme = next;
  try {
    localStorage.setItem("theme", next);
  } catch {
    /* storage unavailable */
  }
  document.dispatchEvent(new CustomEvent("theme-change", { detail: { theme: next } }));
}

function toggleTheme(): void {
  setTheme(theme.value === "dark" ? "light" : "dark");
}

export function useTheme() {
  return { theme, setTheme, toggleTheme };
}
