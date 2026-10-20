export function renderInventory(inventory) {
  document.querySelector("#inventory-heading").textContent = inventory.name;
  document.querySelector("#inventory-summary").textContent =
    `${inventory.hosts.length} host · ${Object.keys(inventory.groups).length} groups · local connection`;
  const hosts = new Map(inventory.hosts.map((host) => [host.name, host]));

  function inspectHost(name) {
    const host = hosts.get(name);
    const title = document.createElement("h3");
    title.textContent = host.name;
    const list = document.createElement("dl");
    list.className = "detail-list";
    for (const [label, value] of [
      ["Inventory", inventory.name],
      ["Connection", host.connection],
      ["Direct groups", host.direct_groups.join(", ") || "None"],
      ["All memberships", host.groups.join(", ") || "None"],
    ]) {
      const row = document.createElement("div");
      const term = document.createElement("dt");
      const description = document.createElement("dd");
      term.textContent = label;
      description.textContent = value;
      row.append(term, description);
      list.append(row);
    }
    document.querySelector("#host-details").replaceChildren(title, list);
    document.querySelectorAll(".host-link").forEach((button) => {
      button.setAttribute("aria-pressed", String(button.dataset.host === name));
    });
  }

  function groupNode(name) {
    const group = inventory.groups[name];
    const item = document.createElement("li");
    const details = document.createElement("details");
    details.open = true;
    const summary = document.createElement("summary");
    summary.append(document.createTextNode(name));
    const count = document.createElement("span");
    count.className = "tree-count";
    count.textContent = `${group.host_count} ${group.host_count === 1 ? "host" : "hosts"}`;
    summary.append(count);
    const children = document.createElement("ul");
    for (const child of group.children) children.append(groupNode(child));
    for (const name of group.hosts) {
      const leaf = document.createElement("li");
      const button = document.createElement("button");
      button.type = "button";
      button.className = "host-link";
      button.dataset.host = name;
      button.textContent = name;
      button.setAttribute("aria-label", `Inspect host ${name}`);
      button.addEventListener("click", () => inspectHost(name));
      leaf.append(button);
      children.append(leaf);
    }
    if (!children.childElementCount) {
      const empty = document.createElement("li");
      empty.className = "tree-empty";
      empty.textContent = "No hosts";
      children.append(empty);
    }
    details.append(summary, children);
    item.append(details);
    return item;
  }

  const tree = document.createElement("ul");
  tree.append(groupNode("all"));
  document.querySelector("#inventory-tree").replaceChildren(tree);
  if (inventory.hosts.length) inspectHost(inventory.hosts[0].name);
}
