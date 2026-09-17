# Masterplan.md
## AI-Powered Room Visualization & Furniture Fitting App

---

## 1. App Overview & Objectives

This app helps homeowners and renters avoid one of the most common and frustrating furniture-buying mistakes: purchasing items online that don't fit, don't look right, or can't be returned once they arrive.

Users record a video of a room using their phone or computer's camera through the web app. The system reconstructs the room as a fully navigable 3D model, digitally clears out any existing furniture, and generates a starter furnished layout using real, purchasable products — all while respecting a budget the user sets. Users can explore the room freely in 3D, swap furniture in and out, and tap any item to jump straight to the real product page to buy it.

**Core objective:** Give users the confidence to buy furniture that actually fits their space and their budget — before they spend a dime.

---

## 2. Target Audience

- **Primary:** Homeowners and renters furnishing or refreshing a room, who are budget-conscious and have been burned before by furniture that didn't fit or match expectations.
- **Secondary (future potential):** Interior design hobbyists, people moving into new homes/apartments, and eventually design professionals looking for a fast client visualization tool.

At launch, this is a personal project aimed at a small audience, with an intentional design approach that allows for gradual growth into a small user base over time.

---

## 3. Core Features & Functionality

### V1 (Launch) Features
1. **Room Video Capture** — Users record a walkthrough video of a room directly through the web app (usable on both desktop and mobile browsers).
2. **3D Room Reconstruction** — The video is processed into a fully navigable 3D model of the room's structure (walls, floor, windows, etc.).
3. **Existing Furniture Removal** — The AI identifies furniture/clutter already in the room and digitally removes it, leaving a clean, empty 3D space.
4. **Dimension Handling** — Users can optionally enter known measurements (e.g., wall length) to improve accuracy; otherwise, the AI estimates room dimensions automatically from the video.
5. **AI-Generated Starter Layout** — The system automatically furnishes the empty room with a complete starter layout of real products, respecting the user's total budget.
6. **Manual Customization** — Users can swap, remove, or add individual furniture pieces to the AI's starter layout, with the budget total updating live.
7. **Tap-to-Shop** — Tapping any furniture item in the 3D scene shows product details and links out to the retailer's website to purchase.
8. **Simple Budget Input** — Users set one overall budget; the app filters/suggests furniture and tracks running spend against it.
9. **Optional Accounts** — Users can sign in with Google to save room scans and furniture layouts for later; the app is also fully usable without an account.
10. **Persistent Storage with User Control** — Room scans are saved by default so users can revisit them later, with the ability to delete their data at any time.

### Later / Post-V1 Features
- **Bring-Your-Own Product Links** — Users paste a specific product URL, and the AI attempts to simulate and place it, flagging if it's not feasible (e.g., dimensions unavailable or item won't fit).
- **Cultural/Religious Placement Rules** — Optional AI-guided layout rules based on user-selected traditions (e.g., Vastu Shastra principles for Hindu users), influencing furniture placement suggestions.

---

## 4. High-Level Technical Stack Recommendations

*(Conceptual only — specific tools/providers to be finalized during implementation planning)*

| Layer | Purpose | Approach |
|---|---|---|
| **Frontend (Web App)** | The interface users interact with, including 3D scene navigation | A modern web framework capable of rendering interactive 3D graphics in-browser |
| **3D Rendering Engine** | Powers the free-roam navigable 3D room experience | A browser-based 3D graphics technology, using simplified/efficient models to keep performance reasonable across devices |
| **Video/Frame Processing** | Extracts clean, usable frames from uploaded room videos | Open-source video processing tools to remove blurry/duplicate frames before 3D reconstruction |
| **3D Reconstruction Pipeline** | Turns processed video frames into a 3D point cloud/model of the room | Open-source computer vision models for point cloud generation and object segmentation (identifying and masking individual objects, similar to the reference pipeline you shared) |
| **Furniture Data Pipeline** | Builds the catalog of placeable furniture | Web scraping system that pulls product data broadly across retailers, filtering out any product missing clear dimensions, then normalizing inconsistent formatting (units, layout of specs) into a consistent structure |
| **Structured Database** | Stores user accounts, budgets, room metadata, furniture catalog data, and links between rooms and placed furniture | A relational or document-based database suited for structured, relational data |
| **File/Media Storage** | Stores uploaded videos, generated 3D models, and product images | Cloud object storage suited for large media files, separate from the structured database |
| **Authentication** | Handles optional user sign-in | "Sign in with Google" (OAuth), avoiding the need to build/manage passwords |
| **Processing Queue** | Manages the (non-instant) room reconstruction workload | An asynchronous job/task queue so video processing happens in the background, with users notified when results are ready |

**Cost-conscious approach:** Given the preference for free/open-source tools, the reconstruction pipeline should prioritize open-source computer vision and 3D modeling models over paid commercial APIs wherever feasible, accepting some additional setup complexity in exchange for near-zero operating costs at this stage.

---

## 5. Conceptual Data Model

At a high level, the app's data can be thought of in these core entities and their relationships:

- **User** (optional) — has an identity (via Google sign-in), and can own multiple Rooms and a Budget preference.
- **Room** — represents one uploaded room scan; contains the raw video reference, the generated 3D model reference, estimated or user-provided dimensions, and a status (processing, ready, etc.). Belongs to a User (or exists anonymously/session-based if no account).
- **Furniture Item (Catalog)** — represents a scraped product; contains dimensions, price, images, retailer link, category/style, and material/type classification.
- **Placed Furniture** — represents a specific Furniture Item placed inside a specific Room, including its position/orientation within the 3D scene. Links a Room to one or more Furniture Items.
- **Budget** — a value associated with a Room (or a session), tracking the target amount and running total of currently placed furniture.
- **(Future) Custom Product Request** — represents a user-submitted product link for the "bring your own link" feature, with a status indicating whether the AI successfully simulated it.
- **(Future) Placement Rule Set** — represents an optional cultural/religious rule profile (e.g., Vastu) that can be applied to influence furniture placement suggestions for a Room.

This structure keeps the "big files" (videos, 3D models, images) referenced from — but stored separately from — the structured relationships between users, rooms, budgets, and furniture.

---

## 6. User Interface & Design Principles

- **Simplicity first:** Given the technical complexity happening behind the scenes (3D reconstruction, AI placement), the interface itself should feel clean, calm, and guided — not overwhelming.
- **3D scene as the centerpiece:** The navigable 3D room should be the visual focus of the results screen, with supporting controls (furniture list, budget tracker, swap options) arranged around it rather than competing with it.
- **Clear process states:** Since processing isn't instant, the app should clearly communicate status (uploading → processing → ready), so users understand what's happening and aren't left guessing.
- **Low-friction entry:** Users should be able to try the core experience (recording a room, seeing a result) without being forced to create an account first; account creation should feel like an optional bonus, not a gate.
- **Budget visibility:** The running budget total should be visible and easy to understand at all times while customizing a room's furniture.
- **Mobile-aware, desktop-first:** Since recording will often happen on a phone browser but design/browsing may happen on a bigger screen, the interface should adapt gracefully across screen sizes.

---

## 7. Security & Privacy Considerations

- **Sensitive content awareness:** Room videos are inherently personal (interiors of people's homes). The app should treat this data with care from day one.
- **User-controlled deletion:** Users must be able to delete their uploaded videos, 3D models, and associated data at any time.
- **Persistent by default:** Videos and scans are retained (not auto-deleted) so users can revisit past rooms without re-recording, balanced against giving them clear, easy deletion controls.
- **Authentication security:** Using "Sign in with Google" offloads password security/management to a trusted provider, reducing the app's own security burden.
- **Data access control:** Room data and saved designs should only be accessible to the user who created them (or kept anonymous/session-based if no account was used).
- **Scraped data hygiene:** Since furniture data comes from general web scraping, product links and data should be handled carefully to avoid exposing users to broken, malicious, or misleading links.

---

## 8. Development Phases & Milestones

**Phase 1: Core Pipeline Proof of Concept**
- Build the video-to-3D-room reconstruction pipeline (frame extraction → point cloud → 3D model)
- Validate accuracy of AI dimension estimation vs. user-provided measurements

**Phase 2: Furniture Data Foundation**
- Build the web scraping and data normalization pipeline
- Establish the filtered, dimension-verified furniture catalog

**Phase 3: Furniture Removal & Placement**
- Implement existing-furniture detection and removal from scanned rooms
- Implement AI-driven starter layout generation within a budget
- Implement manual swap/add/remove customization

**Phase 4: Core Web App Experience**
- Build the web interface: video capture flow, processing/status screens, navigable 3D viewer, budget tracker, tap-to-shop links
- Implement optional Google sign-in and persistent room storage with user-controlled deletion

**Phase 5: Personal Beta & Refinement**
- Use the app yourself and with a small trusted group
- Refine accuracy, performance, and usability based on real usage

**Phase 6 (Later): Feature Expansion**
- Bring-your-own product link simulation
- Cultural/religious placement rule sets (e.g., Vastu)
- Preparation for handling a larger user base, if desired

---

## 9. Potential Challenges & Solutions

| Challenge | Approach |
|---|---|
| **Room dimension accuracy from video alone (no depth sensor)** | Offer optional manual measurement input to calibrate AI estimates; treat AI-only estimates as "best guess" and consider clearly communicating confidence to users |
| **Inconsistent furniture data from general scraping** | Build a normalization step to standardize units/formats, and strictly filter out any product missing clear dimensions |
| **3D scene performance across different devices** | Use simplified/efficient 3D models rather than ultra-high-detail assets, especially at v1 |
| **Processing time for 3D reconstruction** | Design the experience around asynchronous "upload and come back later" expectations, with clear status updates |
| **Free/open-source AI tooling being less polished than paid alternatives** | Budget extra time for setup/tuning of open-source models; treat this as an acceptable trade-off for near-zero costs at this stage |
| **Handling sensitive home footage responsibly** | Persistent storage paired with clear, easy user-controlled deletion; access restricted to the owning user |

---

## 10. Future Expansion Possibilities

- Native mobile apps (leveraging phone depth sensors like LiDAR for more accurate scans) once budget/resources allow
- Retailer partnerships or official APIs to supplement/replace general scraping with more reliable data
- Per-category budget controls (e.g., separate limits for couch, table, rug) instead of just one total
- Social/sharing features (e.g., sharing a furnished room design with friends or family for feedback)
- Interior designer-focused tools/tier, if expanding beyond individual consumers
- Expanded cultural/religious placement rule options beyond the initial set
- In-app checkout or retailer affiliate integration, if the product grows significantly

---

*This masterplan is a living document and can be adjusted as the project evolves, priorities shift, or new constraints/opportunities are discovered.*
