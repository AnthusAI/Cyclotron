const cyclotronTailwindPreset = {
  theme: {
    extend: {
      colors: {
        background: "var(--background)",
        foreground: "var(--foreground)",
        card: "var(--card)",
        "card-foreground": "var(--card-foreground)",
        popover: "var(--popover)",
        "popover-foreground": "var(--popover-foreground)",
        primary: "var(--primary)",
        "primary-foreground": "var(--primary-foreground)",
        secondary: "var(--secondary)",
        "secondary-foreground": "var(--secondary-foreground)",
        muted: "var(--muted)",
        "muted-foreground": "var(--muted-foreground)",
        accent: "var(--accent)",
        "accent-foreground": "var(--accent-foreground)",
        input: "var(--input)",
        destructive: "var(--destructive)",
        success: "var(--success)",
      },
      fontFamily: {
        display: ["Oxanium Variable", "sans-serif"],
        body: ["Roboto Variable", "sans-serif"],
        brand: ["Blinker", "sans-serif"],
      },
    },
  },
};

export default cyclotronTailwindPreset;
