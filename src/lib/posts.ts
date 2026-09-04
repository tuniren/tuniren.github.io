import { getCollection } from 'astro:content';

export async function getPublishedPosts() {
  const posts = await getCollection('blog', ({ data }) => !data.draft);
  return posts.sort((a, b) => b.data.pubDate.valueOf() - a.data.pubDate.valueOf());
}

export async function getAllTags() {
  const posts = await getPublishedPosts();
  return Array.from(new Set(posts.flatMap((post) => post.data.tags))).sort((a, b) =>
    a.localeCompare(b)
  );
}
