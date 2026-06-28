import { describe, it, expect, vi, beforeEach } from "vitest";

vi.mock("../../lib/daemon.js", () => ({
  daemon: { confirm: vi.fn().mockResolvedValue({}), action: vi.fn().mockResolvedValue({ result: "Done" }) },
}));
import { daemon } from "../../lib/daemon.js";
import "./orb-cards.js";

function mountCards() {
  document.body.innerHTML = '<div id="a11y-live"></div><orb-cards id="cards"></orb-cards>';
  return document.getElementById("cards");
}

beforeEach(() => { vi.clearAllMocks(); });

describe("showConfirm", () => {
  it("renders a danger alertdialog with the Confirm title and proceed hint", () => {
    const cards = mountCards();
    cards.showConfirm("Delete file?", "danger");
    const card = cards.querySelector("#confirm-card");
    expect(card.classList.contains("danger")).toBe(true);
    expect(card.querySelector(".ttl").textContent).toBe("Confirm");
    expect(card.querySelector(".msg").textContent).toBe("Delete file?");
    expect(card.querySelector(".hint").textContent).toContain("proceed");
  });

  it("clicking Proceed posts {answer:true} and clears", () => {
    const cards = mountCards();
    cards.showConfirm("Delete?", "danger");
    cards.querySelector(".yes").click();
    expect(daemon.confirm).toHaveBeenCalledWith({ answer: true });
    expect(cards.querySelector("#confirm-card")).toBeNull();
  });

  it("read/write tiers use calmer titles", () => {
    const cards = mountCards();
    cards.showConfirm("x", "read");
    expect(cards.querySelector(".ttl").textContent).toBe("Allow access");
  });
});

describe("showChoices", () => {
  it("renders top 3 items plus a '+N more' hint", () => {
    const cards = mountCards();
    cards.showChoices({ title: "Top", items: [1, 2, 3, 4].map((n) => ({ label: "f" + n, actions: [{ tool: "open", label: "Open" }] })) });
    const card = cards.querySelector("#choices-card");
    expect(card.querySelectorAll(".citem").length).toBe(3);
    expect(card.querySelector(".hint").textContent).toContain("+1 more");
  });

  it("an action button calls daemon.action", () => {
    const cards = mountCards();
    cards.showChoices({ title: "Top", items: [{ label: "f1", actions: [{ tool: "open", args: { i: 0 }, label: "Open" }] }] });
    cards.querySelector(".crow .btn").click();
    expect(daemon.action).toHaveBeenCalledWith("open", { i: 0 });
  });
});

describe("keyboard", () => {
  it("Escape on a confirm card answers false", () => {
    const cards = mountCards();
    cards.showConfirm("Delete?", "danger");
    document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape" }));
    expect(daemon.confirm).toHaveBeenCalledWith({ answer: false });
  });
});
