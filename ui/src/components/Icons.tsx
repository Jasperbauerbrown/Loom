// Inline SVG icons (no icon library, 03 §19). 16px grid, `currentColor`.

type P = { className?: string };

const base = (className?: string) => ({
  className: className ?? "h-4 w-4",
  viewBox: "0 0 16 16",
  fill: "none",
  stroke: "currentColor",
  strokeWidth: 1.5,
  strokeLinecap: "round" as const,
  strokeLinejoin: "round" as const,
  "aria-hidden": true,
});

export const IconDesk = ({ className }: P) => (
  <svg {...base(className)}>
    <path d="M2.5 3.5h11v7h-11z" />
    <path d="M5 13h6M8 10.5V13" />
  </svg>
);

export const IconThreads = ({ className }: P) => (
  <svg {...base(className)}>
    <path d="M3 3.5v6a2 2 0 0 0 2 2h8" />
    <path d="M10.5 9.5 13 11.5l-2.5 2" />
    <path d="M3 3.5h4" />
  </svg>
);

export const IconVault = ({ className }: P) => (
  <svg {...base(className)}>
    <path d="M3 2.5h7.5L13 5v8.5H3z" />
    <path d="M5.5 5.5h4M5.5 8h5M5.5 10.5h3" />
  </svg>
);

export const IconApprovals = ({ className }: P) => (
  <svg {...base(className)}>
    <path d="M8 1.8 13.2 4v4c0 3-2.2 5.2-5.2 6.2C5 13.2 2.8 11 2.8 8V4z" />
    <path d="m5.8 7.9 1.6 1.6 3-3.2" />
  </svg>
);

export const IconSettings = ({ className }: P) => (
  <svg {...base(className)}>
    <circle cx="8" cy="8" r="2.2" />
    <path d="M8 1.6v1.6M8 12.8v1.6M14.4 8h-1.6M3.2 8H1.6M12.5 3.5l-1.1 1.1M4.6 11.4l-1.1 1.1M12.5 12.5l-1.1-1.1M4.6 4.6 3.5 3.5" />
  </svg>
);

export const IconSystem = ({ className }: P) => (
  <svg {...base(className)}>
    <rect x="2.5" y="4" width="11" height="8" rx="1.5" />
    <path d="M5 7h2.5M5 9.5h5" />
  </svg>
);

export const IconChevron = ({ className }: P) => (
  <svg {...base(className)}>
    <path d="m6 3.5 4.5 4.5L6 12.5" />
  </svg>
);

export const IconClose = ({ className }: P) => (
  <svg {...base(className)}>
    <path d="m4 4 8 8M12 4l-8 8" />
  </svg>
);

export const IconPlus = ({ className }: P) => (
  <svg {...base(className)}>
    <path d="M8 3.5v9M3.5 8h9" />
  </svg>
);

export const IconSend = ({ className }: P) => (
  <svg {...base(className)}>
    <path d="M2.5 8 13.5 3l-3 10-2.6-4z" />
  </svg>
);

export const IconRefresh = ({ className }: P) => (
  <svg {...base(className)}>
    <path d="M13 8a5 5 0 1 1-1.6-3.7" />
    <path d="M13.2 2.5v3h-3" />
  </svg>
);

export const IconTrash = ({ className }: P) => (
  <svg {...base(className)}>
    <path d="M3.5 4.5h9M6.5 4.5V3h3v1.5M5 4.5l.5 8h5l.5-8" />
  </svg>
);

export const IconFile = ({ className }: P) => (
  <svg {...base(className)}>
    <path d="M4 2.5h5L12 5.5v8H4z" />
    <path d="M9 2.5v3h3" />
  </svg>
);

export const IconFolder = ({ className }: P) => (
  <svg {...base(className)}>
    <path d="M2.5 4.5h4l1.2 1.5h5.8v7h-11z" />
  </svg>
);

export const IconPaperclip = ({ className }: P) => (
  <svg {...base(className)}>
    <path d="M10.5 5.5 6.2 9.8a1.4 1.4 0 0 0 2 2l5-5a2.8 2.8 0 0 0-4-4L4.1 7.9a4.2 4.2 0 0 0 6 6l3.4-3.4" />
  </svg>
);
