import type { ReactNode, SVGProps } from "react";

/** Hand-drawn 24px line icons (original artwork; no icon library). */
function Base({ children, ...props }: SVGProps<SVGSVGElement> & { children: ReactNode }) {
  return (
    <svg
      width="20"
      height="20"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.7"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
      focusable="false"
      {...props}
    >
      {children}
    </svg>
  );
}

export const SamMark = () => (
  <Base>
    <path d="M16.5 7.5c-1.2-1.3-2.8-2-4.6-2-2.6 0-4.4 1.4-4.4 3.5 0 4.6 8.6 2.6 8.6 7 0 2.2-1.9 3.5-4.7 3.5-2 0-3.7-.7-5-2" />
  </Base>
);
export const IconChat = () => (
  <Base>
    <path d="M4 5.5h16v10H10l-4.5 3.5v-3.5H4z" />
  </Base>
);
export const IconKnowledge = () => (
  <Base>
    <path d="M5 4.5h10.5a2 2 0 0 1 2 2V20H7a2 2 0 0 1-2-2z" />
    <path d="M5 18a2 2 0 0 1 2-2h10.5M9 8.5h5" />
  </Base>
);
export const IconMemory = () => (
  <Base>
    <circle cx="12" cy="12" r="3" />
    <path d="M12 3v3M12 18v3M3 12h3M18 12h3M5.6 5.6l2.1 2.1M16.3 16.3l2.1 2.1M18.4 5.6l-2.1 2.1M7.7 16.3l-2.1 2.1" />
  </Base>
);
export const IconTools = () => (
  <Base>
    <path d="M14.5 6.5a4 4 0 0 0-5 5L4 17l3 3 5.5-5.5a4 4 0 0 0 5-5l-2.5 2.5-2-.5-.5-2z" />
  </Base>
);
export const IconShield = () => (
  <Base>
    <path d="M12 3l7 3v5c0 4.5-3 8-7 10-4-2-7-5.5-7-10V6z" />
    <path d="M9 12l2 2 4-4" />
  </Base>
);
export const IconActivity = () => (
  <Base>
    <path d="M3 12h4l2.5-6 4 12 2.5-6H21" />
  </Base>
);
export const IconSettings = () => (
  <Base>
    <circle cx="12" cy="12" r="3" />
    <path d="M19 12a7 7 0 0 0-.1-1.2l2-1.5-2-3.4-2.3.9a7 7 0 0 0-2-1.2L14 3h-4l-.6 2.6a7 7 0 0 0-2 1.2l-2.3-.9-2 3.4 2 1.5A7 7 0 0 0 5 12c0 .4 0 .8.1 1.2l-2 1.5 2 3.4 2.3-.9a7 7 0 0 0 2 1.2L10 21h4l.6-2.6a7 7 0 0 0 2-1.2l2.3.9 2-3.4-2-1.5c.1-.4.1-.8.1-1.2z" />
  </Base>
);
export const IconMic = () => (
  <Base>
    <rect x="9" y="3" width="6" height="11" rx="3" />
    <path d="M5.5 11.5a6.5 6.5 0 0 0 13 0M12 18v3" />
  </Base>
);
export const IconStop = () => (
  <Base>
    <rect x="7" y="7" width="10" height="10" rx="2" />
  </Base>
);
export const IconSend = () => (
  <Base>
    <path d="M4 12l16-8-6 16-3-6.5z" />
  </Base>
);
export const IconSpeaker = () => (
  <Base>
    <path d="M4 9.5h3.5L12 6v12l-4.5-3.5H4z" />
    <path d="M15.5 9a4 4 0 0 1 0 6M18 6.5a7.5 7.5 0 0 1 0 11" />
  </Base>
);
export const IconUpload = () => (
  <Base>
    <path d="M12 16V5M7.5 9.5L12 5l4.5 4.5M5 19h14" />
  </Base>
);
export const IconTrash = () => (
  <Base>
    <path d="M5 7h14M10 4h4M7 7l1 13h8l1-13M10 11v6M14 11v6" />
  </Base>
);
export const IconSidebar = () => (
  <Base>
    <rect x="3.5" y="4.5" width="17" height="15" rx="3" />
    <path d="M9.5 4.5v15" />
  </Base>
);
export const IconSearch = () => (
  <Base>
    <circle cx="11" cy="11" r="6" />
    <path d="M16 16l4 4" />
  </Base>
);
export const IconPlus = () => (
  <Base>
    <path d="M12 5v14M5 12h14" />
  </Base>
);
export const IconArrowUp = () => (
  <Base>
    <path d="M12 19V5M6 11l6-6 6 6" />
  </Base>
);
export const IconPanelLeft = () => (
  <Base>
    <rect x="3.5" y="4.5" width="17" height="15" rx="3" />
    <path d="M9.5 4.5v15" />
  </Base>
);
export const IconPanelRight = () => (
  <Base>
    <rect x="3.5" y="4.5" width="17" height="15" rx="3" />
    <path d="M14.5 4.5v15" />
  </Base>
);
export const IconSparkle = () => (
  <Base width="26" height="26">
    <path d="M12 3l1.9 5.1L19 10l-5.1 1.9L12 17l-1.9-5.1L5 10l5.1-1.9z" />
    <path d="M18.5 16.5l.7 1.8 1.8.7-1.8.7-.7 1.8-.7-1.8-1.8-.7 1.8-.7z" />
  </Base>
);
