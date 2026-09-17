- Read Masterplan.md and implementation-plan.md before making product-specific design decisions. Treat them as the source of truth.
- Before building the landing page, show me the proposed RoomFittr design system so I can approve it.
Build a minimal, premium landing page for an AI-powered room visualization app that turns a room video into a navigable 3D room and helps users visualize real furniture that fits their space and budget. .
THE ONE FEELING: Calm and Modern feeling. Every choice serves this. If something does not, cut it. When in doubt, remove, do not add.
STACK
- Next.js App Router with TypeScript
- Tailwind CSS
- React
- Motion for animation
- Use the 21st.dev Magic Chat MCP for structural components when useful, then restyle them completely to match the RoomFittr design system
- Use the UI/UX Pro Max skill to define and lock the design system before building
- Use Superpowers to plan, scaffold, run, inspect, and review the implementation
- Use Three.js / React Three Fiber where appropriate for the 3D room product preview
- Do not add unnecessary libraries or dependencies








DEFAULT TEXT (follow this exactly, it is where these builds usually fail)
Two fonts total: one display font for headlines, one clean readable font for everything else. Not Inter, Roboto, or Arial. Use Century Gothic font for the headlines and Nunito font for everything else
Headlines in sentence case or normal case. Do NOT force all-caps and do NOT use small-caps. Never set a serif font in all-caps. It looks dated and stiff.
Body text: 16 to 18px, line height around 1.6, max line length about 65 characters. Never let a paragraph run the full page width
High contrast. Body text must pass WCAG AA against its background. No gray-on-background mush.
DESIGN SYSTEM (lock before building sections)
One dominant color, one accent, used sparingly. No third color. The dominant color here is pastel blue and the secondary color is hurricane grey. 
Lock a spacing scale and stick to it. Extreme negative space. Add room, not content.
- Use consistent border radii, shadows, borders, and component heights.
- Every section should feel balanced across the full width.
- Avoid unnecessary gradients, excessive glassmorphism, floating blobs, or decorative elements.
- Avoid the generic "AI startup" visual style.
Every section fills the width with balance. Never strand all the content in one third with an empty half.
SECTIONS (keep it lean, in order)
Sticky Navbar
Hero
3D Room Product Preview
How It Works
Key Features
Why RoomFittr
FAQ
Final CTA
Footer
MOTION (less is more)
Pick 2 to 3 high-impact moments and choreograph them well: a staggered hero entrance, gentle scroll reveals as sections enter (whileInView, once: true), and one signature hover.
Skip animation everywhere else. Scattered motion reads cheap. Respect prefers-reduced-motion.
Visual Direction
The 3D room should feel:
- Clean
- Modern
- Minimal
- Spatial
- Realistic enough to understand
- Lightweight enough to perform well

Avoid making the landing page look like a video game or a technical CAD application.

The room should support the product story rather than become decoration.

 RESPONSIVE DESIGN

Design desktop-first for a wide screen.

Then ensure the experience remains strong on:
- Laptop
- Tablet
- Mobile

The 3D product preview must remain understandable on smaller screens.

Do not simply stack the desktop layout onto mobile. Adapt the composition intentionally.
		
CHECK YOUR OWN WORK (after every section, before showing me)
Run the site, open localhost:3000, and screenshot the section you just built.
Critique the screenshot honestly: Is the layout balanced or is one side empty? Anything crowded, overlapping, or misaligned? Is the text readable? Does the type look intentional?
Fix every problem, screenshot again, and only show me once it actually looks right.
Confirm there are no runtime or build errors before moving on. A broken build means the page is not rendering what you think it is.
QUALITY BAR
One idea per section. Generous space between every block. No two sections overlap or bleed together.
Desktop-first. Design for a wide screen, then make sure it still holds up smaller.
Lighthouse 90+ on performance.
Copy sounds human. No em-dashes.
PROCESS
Lock the design system and the default text rules first.
Build one section at a time. Screenshot, self-critique, fix, then show me. Move on only when it looks right.
