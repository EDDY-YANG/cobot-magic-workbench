// Conway B3/S23, finite grid with empty boundaries. No robot data or side effects.
export function evolve(cells, width, height) {
  if (cells.length !== width * height) throw new RangeError('Life grid dimensions disagree');
  const next = new Uint8Array(cells.length);
  for (let y = 0; y < height; y++) {
    for (let x = 0; x < width; x++) {
      let neighbors = 0;
      for (let dy = -1; dy <= 1; dy++) for (let dx = -1; dx <= 1; dx++) {
        const xx = x + dx, yy = y + dy;
        if ((dx || dy) && xx >= 0 && xx < width && yy >= 0 && yy < height) neighbors += cells[yy * width + xx];
      }
      const i = y * width + x;
      next[i] = Number(neighbors === 3 || (cells[i] === 1 && neighbors === 2));
    }
  }
  return next;
}

export function seed(width = 84, height = 26) {
  const cells = new Uint8Array(width * height);
  const put = (points, ox, oy) => points.forEach(([x, y]) => {
    if (x + ox >= 0 && x + ox < width && y + oy >= 0 && y + oy < height) cells[(y + oy) * width + x + ox] = 1;
  });
  // Standard Gosper glider gun and pulsar, evolved entirely by the rule above.
  put([[24,0],[22,1],[24,1],[12,2],[13,2],[20,2],[21,2],[34,2],[35,2],
    [11,3],[15,3],[20,3],[21,3],[34,3],[35,3],[0,4],[1,4],[10,4],[16,4],[20,4],[21,4],
    [0,5],[1,5],[10,5],[14,5],[16,5],[17,5],[22,5],[24,5],[10,6],[16,6],[24,6],
    [11,7],[15,7],[12,8],[13,8]], 4, 3);
  const pulsar = [];
  for (const y of [0,5,7,12]) for (const x of [2,3,4,8,9,10]) pulsar.push([x,y]);
  for (const y of [2,3,4,8,9,10]) for (const x of [0,5,7,12]) pulsar.push([x,y]);
  put(pulsar, 60, 7);
  return cells;
}
