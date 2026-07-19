import axios from 'axios';

/**
 * API client for monitored Trakt/Letterboxd list management.
 */
export const listsApi = {
  /**
   * Get all monitored lists.
   * @returns {Promise<Object>} Response with a `lists` array.
   */
  async getLists() {
    const response = await axios.get('/api/lists');
    return response.data;
  },

  /**
   * Add a monitored list.
   * @param {Object} data - { name, source, url, media_type?, quality?,
   *   collection_enabled, sync_interval_hours }.
   * @returns {Promise<Object>} Response with the created `list`.
   */
  async addList(data) {
    const response = await axios.post('/api/lists', data);
    return response.data;
  },

  /**
   * Remove a monitored list (and its Plex collection).
   * @param {number} listId - List ID.
   * @returns {Promise<Object>} Response with status.
   */
  async removeList(listId) {
    const response = await axios.delete(`/api/lists/${listId}`);
    return response.data;
  },

  /**
   * Sync a single monitored list now.
   * @param {number} listId - List ID.
   * @returns {Promise<Object>} Response with a `summary`.
   */
  async syncList(listId) {
    const response = await axios.post(`/api/lists/${listId}/sync`);
    return response.data;
  },
};

export default listsApi;
