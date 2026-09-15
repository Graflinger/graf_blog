const { HtmlBasePlugin } = require('@11ty/eleventy');
const { execSync } = require('child_process');
const electricity = require('./src/js/dashboards/electricity-data');
const { validateBuildSnapshot } = require('./src/data_ingestion/builders/electricitySnapshot');
const { verifyPublished } = require('./src/data_ingestion/builders/electricityHistory');
const { verifyPublishedTrends } = require('./src/data_ingestion/builders/electricityTrends');
const { verifyPublishedProgress } = require('./src/data_ingestion/builders/electricityProgress');
const electricityFilters = require('./src/data_ingestion/builders/electricityFilters');

module.exports = function (eleventyConfig) {
  // Generate charts before Eleventy build
  eleventyConfig.on('eleventy.before', async () => {
    console.log('🎨 Generating charts...');
    execSync('node "src/data_ingestion/generate-charts.js"', {
      stdio: 'inherit',
    });
  });

  eleventyConfig.addFilter('electricitySummary', (snapshot) => {
    validateBuildSnapshot(snapshot);
    return electricityFilters.summary(snapshot);
  });
  eleventyConfig.addFilter('electricityNumber', electricity.number);
  eleventyConfig.addFilter('electricityStatusJSON', electricityFilters.statusJSON);
  eleventyConfig.addFilter('electricityJSON', (snapshot) => {
    validateBuildSnapshot(snapshot);
    return JSON.stringify(snapshot).replace(/</g, '\\u003c').replace(/>/g, '\\u003e').replace(/&/g, '\\u0026');
  });

  // Ignore data ingestion folder
  eleventyConfig.ignores.add('src/data_ingestion/**');
  eleventyConfig.addPassthroughCopy({ 'src/data-history/german-electricity': 'data/history/german-electricity' });
  eleventyConfig.addPassthroughCopy('src/_headers');
  eleventyConfig.on('eleventy.after', ({ dir }) => verifyPublished(dir.output));
  eleventyConfig.on('eleventy.after', ({ dir }) => verifyPublishedTrends(dir.output));
  eleventyConfig.on('eleventy.after', ({ dir }) => verifyPublishedProgress(dir.output));

  // Exclude generated charts from watch to prevent rebuild loop
  eleventyConfig.watchIgnores.add('src/js/charts/**');

  // Copy the CSS directory to output
  eleventyConfig.addPassthroughCopy('src/css');

  // Add HTML base plugin to manage base URLs
  eleventyConfig.addPlugin(HtmlBasePlugin);

  // Copy the JS directory to output
  eleventyConfig.addPassthroughCopy('src/js');

  // Copy the images directory to output
  eleventyConfig.addPassthroughCopy('src/images');

  // Copy robots.txt to output
  eleventyConfig.addPassthroughCopy('src/robots.txt');

  // Copy ads.txt to output
  eleventyConfig.addPassthroughCopy('src/ads.txt');

  // Copy ECharts library to output
  eleventyConfig.addPassthroughCopy({
    'node_modules/echarts/dist/echarts.min.js': 'js/lib/echarts.min.js',
  });

  // Add a date filter for formatting dates
  eleventyConfig.addFilter('localDate', function (date, locale = 'en-US') {
    return new Date(date).toLocaleDateString(locale, {
      year: 'numeric',
      month: 'long',
      day: 'numeric',
    });
  });

  // Add ISO date string filter for meta tags
  eleventyConfig.addFilter('htmlDateString', function (date) {
    return new Date(date).toISOString();
  });

  // Filter to get related posts
  eleventyConfig.addFilter('relatedPosts', function (collections, currentPage, currentTopics) {
    const allPosts = collections.post || [];
    const topics = currentTopics || [];
    const currentUrl = (currentPage && currentPage.url) || '';

    // Get posts from the same topic (excluding current post)
    const sameTopic = allPosts.filter((post) => {
      if (post.url === currentUrl) return false;
      const postTopics = post.data.topic || [];
      return postTopics.some((topic) => topics.includes(topic));
    });

    // Sort by date (newest first) and get latest 10
    const latest10SameTopic = sameTopic
      .sort((a, b) => new Date(b.data.date) - new Date(a.data.date))
      .slice(0, 10);

    // Randomly select 3 from the latest 10
    const shuffled = latest10SameTopic.sort(() => 0.5 - Math.random());
    const randomThree = shuffled.slice(0, 3);

    // Get the newest post from all posts (excluding current post and same topic)
    const newestAcrossTopics = allPosts
      .filter(
        (post) =>
          post.url !== currentUrl && !sameTopic.some((samePost) => samePost.url === post.url),
      )
      .sort((a, b) => new Date(b.data.date) - new Date(a.data.date))[0];

    // Combine: 3 random from same topic + 1 newest across topics
    const result = [...randomThree];
    if (newestAcrossTopics) {
      result.push(newestAcrossTopics);
    }

    return result;
  });

  // Add global data for helpers
  eleventyConfig.addGlobalData('helpers', {
    year: new Date().getFullYear(),
  });

  // Create main post collection from all posts in src/posts
  eleventyConfig.addCollection('post', function (collectionApi) {
    return collectionApi.getFilteredByGlob('src/posts/**/*.md');
  });

  // Create topic-specific collections
  eleventyConfig.addCollection('energiePosts', function (collectionApi) {
    return collectionApi.getFilteredByGlob('src/posts/**/*.md').filter((post) => {
      return post.data.topic && post.data.topic.includes('energie');
    });
  });

  eleventyConfig.addCollection('politikPosts', function (collectionApi) {
    return collectionApi.getFilteredByGlob('src/posts/**/*.md').filter((post) => {
      return post.data.topic && post.data.topic.includes('politik-und-gesellschaft');
    });
  });

  eleventyConfig.addCollection('wirtschaftPosts', function (collectionApi) {
    return collectionApi.getFilteredByGlob('src/posts/**/*.md').filter((post) => {
      return post.data.topic && post.data.topic.includes('wirtschaft');
    });
  });

  return {
    dir: {
      input: 'src',
      output: '_site',
      includes: '_includes',
      layouts: '_includes',
    },
  };
};
