import js from "@eslint/js";
import reactHooks from "eslint-plugin-react-hooks";
import globals from "globals";
import tseslint from "typescript-eslint";

export default tseslint.config(
  { ignores: ["dist", "node_modules", "src-tauri"] },
  js.configs.recommended,
  ...tseslint.configs.recommended,
  {
    files: ["src/**/*.{ts,tsx}"],
    languageOptions: { globals: globals.browser },
    plugins: { "react-hooks": reactHooks },
    rules: {
      ...reactHooks.configs.recommended.rules,
      // Fetch-on-mount via the bridge is intentional (external system sync).
      "react-hooks/set-state-in-effect": "off",
      // Security hygiene: UI content is untrusted, never interpreted as code/HTML.
      "no-eval": "error",
      "no-implied-eval": "error",
      "no-new-func": "error",
      "no-restricted-syntax": [
        "error",
        {
          selector: "JSXAttribute[name.name='dangerouslySetInnerHTML']",
          message: "Untrusted content must never be rendered as HTML.",
        },
        {
          selector:
            "AssignmentExpression[left.property.name=/^(innerHTML|outerHTML)$/]",
          message: "Do not assign HTML strings.",
        },
      ],
    },
  },
);
